from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter
from tripchord.agents.flexible_live_system import FlexibleLiveAgentRun
from tripchord.agents.live_done_gate_v4 import (
    LiveV4DoneGateReport,
    evaluate_live_v4_done_gate,
)
from tripchord.agents.live_jobs import (
    LivePlanningJobSnapshot,
    LivePlanningJobState,
)
from tripchord.agents.live_system import (
    LiveEventReplanRun,
    LiveEvidenceScope,
    LivePackageAgentRun,
)
from tripchord.api import (
    LiveFlexibleFromTextPlanningRequest,
    StartLiveFlexibleFromTextJobResponse,
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
from tripchord.planning.stay_plans import (
    system_stay_plan_candidate_set,
)
from tripchord.runtime_provenance import validate_runtime_provenance

try:
    from benchmarks.run_live_done_gate import (
        _headers,
        _preflight_companion,
    )
except ModuleNotFoundError:
    # Running this file directly places ``benchmarks/`` rather than the
    # repository root on sys.path, so fall back to the sibling module.
    from run_live_done_gate import (  # type: ignore[import-not-found,no-redef]
        _headers,
        _preflight_companion,
    )

_FROM_TEXT_ENDPOINT = "/api/v1/agents/live-flexible-plan-from-text"
_FROM_TEXT_JOBS_ENDPOINT = f"{_FROM_TEXT_ENDPOINT}/jobs"
_EVIDENCE_SCHEMA_VERSION = "tripchord-live-evidence-v4"
_EXPECTED_PROFILE_ID = "tripchord:maldives-free-travel:live-v4"
_FROZEN_MAXIMUM_QUOTE_AGE_MINUTES = 15
_FROZEN_MINIMUM_RECOMMENDABLE_OPTIONS = 2
_MINIMUM_CLIENT_TIMEOUT_MARGIN_SECONDS = 300.0
_JOB_POLL_INTERVAL_SECONDS = 5.0
_CANCELLATION_TIMEOUT_SECONDS = 15.0
_ATTEMPT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_URL_WITH_QUERY_PATTERN = re.compile(r"(?:https?://|/)[^\s\"'<>]*\?[^\s\"'<>]*")
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "api_token",
        "bridge_token",
        "browser_bridge_token",
        "client_secret",
        "secret",
    }
)
_TERMINAL_JOB_STATES = frozenset(
    {
        LivePlanningJobState.SUCCEEDED,
        LivePlanningJobState.FAILED,
        LivePlanningJobState.CANCELLED,
    }
)
_SYNTHETIC_DONE_GATE_EVENT_SOURCE = "tripchord-synthetic-done-gate-fault-injection"
_SYNTHETIC_FAULT_CLAIM_BOUNDARY = (
    "该 sold_out 是 Done-Gate 主动注入的假设故障，不是平台售罄信号或平台售罄证据；"
    "实时外部证据只用于证明同一 provider 的精确分段被只读重查，并找到身份可区分、"
    "仍可用且通过 Repair、Event ReVerifier、独立审计与主控裁决的替代商品。"
)
_DEFAULT_SCENARIO = Path(__file__).parent / "scenarios" / "live-hgh-mle-aug-2026-v4.json"
_DEFAULT_OUTPUT = Path(__file__).parent / "results" / "live-done-gate-v4.json"
_RUNTIME_EVIDENCE_FIELDS = (
    "codex_runtime_dependency",
    "chatgpt_runtime_dependency",
    "model_enabled",
    "model_required",
    "model_provider",
    "primary_model",
    "fast_model",
    "model_trace_count",
    "effective_flexible_timeout_seconds",
    "rag_enabled",
    "runtime_provenance",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the authenticated TripChord live-v4 stay-plan Done-Gate.",
    )
    parser.add_argument("--request", type=Path, default=_DEFAULT_SCENARIO)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--api-token", default="")
    parser.add_argument(
        "--bridge-token",
        default=os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", ""),
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Client-side async-job polling budget. Defaults to the frozen server "
            "execution budget plus 300 seconds and may not be set below that boundary."
        ),
    )
    parser.add_argument("--maximum-quote-age-minutes", type=int, default=15)
    parser.add_argument("--minimum-recommendable-options", type=int, default=2)
    parser.add_argument(
        "--require-model-enhancement",
        action="store_true",
        help=(
            "Require the API run to use the configured model-backed Agent stages. "
            "The default preserves the deterministic-only live-v4 gate contract."
        ),
    )
    return parser.parse_args()


def _load_request(path: Path) -> dict[str, Any]:
    return TypeAdapter(dict[str, Any]).validate_python(json.loads(path.read_text(encoding="utf-8")))


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _utc_now() -> datetime:
    return datetime.now(UTC)

# Environment variables that redirect ``git -C <root>`` to a different
# repository.  Stripped before any git call so the evidence names the repo that
# actually ran, never one injected through the caller's environment
# (GIT_DIR / GIT_WORK_TREE override risk).
_GIT_ENV_OVERRIDES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
    }
)


def _git_safe_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}
    }


def _repo_revision_snapshot() -> dict[str, Any]:
    """One safe, un-redirected snapshot of the authoritative repository.

    ``porcelain`` is kept for start/end comparison and stripped before bundling.
    """
    revision: dict[str, Any] = {
        "toplevel": None,
        "branch": None,
        "commit_sha": None,
        "worktree_dirty": True,
        "porcelain": "",
    }
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=_REPO_ROOT,
            env=_git_safe_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            env=_git_safe_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=_REPO_ROOT,
            env=_git_safe_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT,
            env=_git_safe_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        # Git unavailable means the revision cannot be proven; fail closed.
        return revision
    revision["toplevel"] = toplevel.stdout.strip() or None
    revision["branch"] = branch.stdout.strip() or None
    revision["commit_sha"] = head.stdout.strip() or None
    revision["worktree_dirty"] = bool(status.stdout.strip())
    revision["porcelain"] = status.stdout
    return revision


def _repo_revision(start: dict[str, Any] | None = None) -> dict[str, Any]:
    """Name the repo revision this evidence was produced against.

    Keeps layer-6 evidence cross-checkable against the product Done-Gate
    report: ``commit_sha`` must equal the revision that actually ran, and a
    dirty worktree (``worktree_dirty=true``) voids the revision mapping because
    the running code differs from ``HEAD``.

    When ``start`` is the snapshot captured at the beginning of the run, the
    end snapshot is compared to it; any HEAD move or worktree change sets
    ``revision_changed_during_run=true`` (with the start revision embedded) so
    the evidence fails closed instead of naming a revision that changed while
    the run was in flight (TOCTOU).
    """
    end = _repo_revision_snapshot()
    if start is None:
        # Full snapshot (incl. porcelain) used as the run's start marker.
        return end
    changed = (
        end.get("commit_sha") != start.get("commit_sha")
        or end.get("toplevel") != start.get("toplevel")
        or end.get("porcelain") != start.get("porcelain")
    )
    public: dict[str, Any] = {
        "toplevel": end.get("toplevel"),
        "branch": end.get("branch"),
        "commit_sha": end.get("commit_sha"),
        "worktree_dirty": end.get("worktree_dirty"),
    }
    if changed:
        public["revision_changed_during_run"] = True
        public["start_revision"] = {
            key: start.get(key)
            for key in ("toplevel", "branch", "commit_sha", "worktree_dirty")
        }
    return public


def _safe_response_json(response: httpx.Response, label: str) -> dict[str, Any]:
    """Parse successful JSON without copying arbitrary upstream bodies into errors."""

    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc
    try:
        return TypeAdapter(dict[str, Any]).validate_python(payload)
    except ValueError as exc:
        raise RuntimeError(f"{label} returned a non-object JSON payload") from exc


def _existing_bundle_passed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = TypeAdapter(dict[str, Any]).validate_python(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return False
    done_gate = payload.get("done_gate")
    return isinstance(done_gate, dict) and done_gate.get("passed") is True


def _evidence_output_path(
    requested: Path,
    *,
    passed: bool,
    captured_at: datetime,
) -> Path:
    if passed or not _existing_bundle_passed(requested):
        return requested
    timestamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = requested.suffix or ".json"
    return requested.with_name(f"{requested.stem}.failed-{timestamp}{suffix}")


def _write_evidence_bundle(
    requested: Path,
    bundle: dict[str, Any],
    *,
    passed: bool,
    captured_at: datetime,
) -> Path:
    output = _evidence_output_path(
        requested,
        passed=passed,
        captured_at=captured_at,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
        os.chmod(output, 0o600)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _failure_evidence_bundle(
    *,
    request: dict[str, Any] | None,
    stage: str,
    error: Exception,
    captured_at: datetime,
    context: dict[str, Any],
    repo_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "run_status": "failed_before_done_gate",
        "captured_at": captured_at.isoformat(),
        "repo_revision": repo_revision if repo_revision is not None else _repo_revision(),
        "failure": {
            "stage": stage,
            "type": type(error).__name__,
            "message": str(error),
            "retry_policy": (
                "登录、验证码和合同校验失败不自动重试；外部状态恢复后重新执行，"
                "瞬时浏览器失败由 Source Agent 的有界重试合同处理"
            ),
        },
        **context,
    }
    if request is not None:
        bundle["scenario_sha256"] = _canonical_sha256(request)
        bundle["request"] = request
    return bundle


def _completed_evidence_bundle(
    *,
    request: dict[str, Any],
    report: LiveV4DoneGateReport,
    captured_at: datetime,
    context: dict[str, Any],
    repo_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a completed run without dropping pre-run evidence context."""

    return {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "run_status": "completed" if report.passed else "done_gate_failed",
        "captured_at": captured_at.isoformat(),
        "scenario_sha256": _canonical_sha256(request),
        "repo_revision": repo_revision if repo_revision is not None else _repo_revision(),
        "request": request,
        **context,
        "done_gate": report.model_dump(mode="json"),
    }


def _normalized_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _is_sensitive_field_name(value: object) -> bool:
    normalized = _normalized_field_name(value)
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    return any(
        normalized.endswith(suffix)
        for suffix in ("_password", "_secret", "_token", "_cookie", "_api_key")
    )


def _redact_url_query(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    redacted_query = urlencode(
        [(key, "[REDACTED]") for key, _value in parse_qsl(parsed.query, keep_blank_values=True)],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, parsed.fragment))


def _redact_string(value: str, active: tuple[str, ...]) -> str:
    redacted = value
    for secret in active:
        redacted = redacted.replace(secret, "[REDACTED]")
    return _URL_WITH_QUERY_PATTERN.sub(
        lambda match: _redact_url_query(match.group(0)),
        redacted,
    )


def _redact_explicit_secrets(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively remove credentials and URL query secrets from evidence."""

    active = tuple(secret for secret in secrets if secret)
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _is_sensitive_field_name(key)
                else _redact_explicit_secrets(item, active)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_explicit_secrets(item, active) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_explicit_secrets(item, active) for item in value)
    if isinstance(value, str):
        return _redact_string(value, active)
    return value


def _runner_secrets(args: argparse.Namespace) -> tuple[str, ...]:
    return (
        str(getattr(args, "api_token", "")),
        str(getattr(args, "bridge_token", "")),
    )


async def _runtime_evidence(
    client: httpx.AsyncClient,
    base: str,
    *,
    label: str,
) -> dict[str, Any]:
    """Capture only non-secret runtime identity and trace-counter evidence."""

    response = await client.get(f"{base}/api/v1/agents/runtime")
    payload = _safe_response_json(response, label)
    return {field: payload.get(field) for field in _RUNTIME_EVIDENCE_FIELDS}


def _validate_required_model_runtime(
    runtime: dict[str, Any],
    *,
    require_model_enhancement: bool,
) -> None:
    """Fail before live search when the required model runtime is not strict."""

    if not require_model_enhancement:
        return
    invalid_fields = tuple(
        field for field in ("model_enabled", "model_required") if runtime.get(field) is not True
    )
    if invalid_fields:
        requirements = "; ".join(f"runtime.{field} must be true" for field in invalid_fields)
        raise RuntimeError(
            "--require-model-enhancement runtime preflight failed before "
            f"live search: {requirements}"
        )


def _validate_runtime_timeout_contract(runtime: dict[str, Any]) -> None:
    observed = runtime.get("effective_flexible_timeout_seconds")
    if observed != 3_600:
        raise RuntimeError(
            "live-v4 runtime preflight failed before live search: "
            "runtime.effective_flexible_timeout_seconds must equal 3600; "
            f"observed {observed!r}"
        )


def _validate_runtime_provenance(runtime: dict[str, Any]) -> None:
    """Fail before live search when the running API is not executing HEAD.

    The running API reports its *startup* provenance (repo toplevel, commit
    SHA, dependency-lock fingerprint, live_system source fingerprint).  It is
    compared against the provenance the current checked-out tree claims, so a
    worker started before a HEAD move, or whose on-disk source changed without
    a restart, hard-fails here (exit 2) — the E2E cannot certify code it did
    not actually run.
    """
    mismatches = validate_runtime_provenance(runtime, repo_root=_REPO_ROOT)
    if mismatches:
        raise RuntimeError(
            "live-v4 runtime provenance preflight failed before live search: "
            + "; ".join(mismatches)
        )


def _validate_model_trace_receipt(
    snapshot: LivePlanningJobSnapshot,
    response_payload: dict[str, Any],
    *,
    api_payload_sha256: str,
    require_model_enhancement: bool,
) -> dict[str, Any]:
    """Validate the job-bound trace receipt; global counters are diagnostic only."""

    receipt = {
        "scope_sha256": getattr(snapshot, "model_trace_scope_sha256", None),
        "total_count": getattr(snapshot, "model_trace_count", None),
        "success_count": getattr(snapshot, "model_trace_success_count", None),
        "failure_count": getattr(snapshot, "model_trace_failure_count", None),
    }
    if receipt["scope_sha256"] != api_payload_sha256:
        raise RuntimeError(
            "live-v4 terminal model trace scope is not bound to the canonical API payload"
        )
    total_count = receipt["total_count"]
    success_count = receipt["success_count"]
    failure_count = receipt["failure_count"]
    counts = (total_count, success_count, failure_count)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise RuntimeError("live-v4 terminal model trace receipt has invalid counters")
    assert isinstance(total_count, int) and not isinstance(total_count, bool)
    assert isinstance(success_count, int) and not isinstance(success_count, bool)
    assert isinstance(failure_count, int) and not isinstance(failure_count, bool)
    if total_count != success_count + failure_count:
        raise RuntimeError("live-v4 terminal model trace counters do not reconcile")
    result_receipt = {
        "scope_sha256": response_payload.get("model_trace_scope_sha256"),
        "total_count": response_payload.get("model_trace_count"),
        "success_count": response_payload.get("model_trace_success_count"),
        "failure_count": response_payload.get("model_trace_failure_count"),
    }
    if result_receipt != receipt:
        raise RuntimeError(
            "live-v4 result model trace receipt does not match its terminal job snapshot"
        )
    if require_model_enhancement and success_count <= 0:
        raise RuntimeError(
            "--require-model-enhancement postflight failed: "
            "job-bound model_trace_success_count must be greater than zero; "
            f"observed {success_count}"
        )
    return receipt


def _validate_request_contract(request: dict[str, Any]) -> None:
    profile = TypeAdapter(dict[str, Any]).validate_python(request.get("stay_plan_profile"))
    done_gate_profile = TypeAdapter(dict[str, Any]).validate_python(
        request.get("done_gate_profile")
    )
    expected = system_stay_plan_candidate_set()
    errors: list[str] = []
    if request.get("schema_version") != _EVIDENCE_SCHEMA_VERSION:
        errors.append("schema_version 不是 tripchord-live-evidence-v4")
    if request.get("coverage_mode") != "strict":
        errors.append("coverage_mode 必须为 strict")
    if request.get("max_pairs") != 3:
        errors.append("max_pairs 必须为 3")
    if request.get("timeout_seconds") != 120:
        errors.append("timeout_seconds 必须为 120")
    if request.get("total_timeout_seconds") != 3_600:
        errors.append("total_timeout_seconds 必须为 3600")
    if done_gate_profile.get("maximum_quote_age_minutes") != _FROZEN_MAXIMUM_QUOTE_AGE_MINUTES:
        errors.append("done_gate_profile.maximum_quote_age_minutes 必须为冻结值 15")
    if (
        done_gate_profile.get("minimum_recommendable_options")
        != _FROZEN_MINIMUM_RECOMMENDABLE_OPTIONS
    ):
        errors.append("done_gate_profile.minimum_recommendable_options 必须为冻结值 2")
    if profile.get("profile_id") != _EXPECTED_PROFILE_ID:
        errors.append("stay_plan_profile.profile_id 不匹配")
    if profile.get("expected_candidate_set_sha256") != expected.candidate_set_sha256:
        errors.append("场景中的候选集 SHA 与代码冻结候选集不匹配")
    if profile.get("required_stay_plan_ids") != [item.value for item in expected.stay_plan_ids]:
        errors.append("场景中的住宿方案成员或顺序不匹配")
    if profile.get("scan_limit_per_platform") != 12:
        errors.append("scan_limit_per_platform 必须为预冻结值 12")
    if profile.get("minimum_exact_providers_per_selected_segment") != 2:
        errors.append("minimum_exact_providers_per_selected_segment 必须为场景冻结值 2")
    if errors:
        raise RuntimeError("live-v4 request contract failed before search: " + "；".join(errors))


def _frozen_done_gate_thresholds(
    request: dict[str, Any],
    *,
    maximum_quote_age_minutes: int,
    minimum_recommendable_options: int,
) -> tuple[int, int]:
    """Bind CLI compatibility flags to the pre-search frozen scenario values."""

    _validate_request_contract(request)
    profile = TypeAdapter(dict[str, int]).validate_python(request["done_gate_profile"])
    frozen_maximum_age = profile["maximum_quote_age_minutes"]
    frozen_minimum_options = profile["minimum_recommendable_options"]
    errors: list[str] = []
    if maximum_quote_age_minutes != frozen_maximum_age:
        errors.append("--maximum-quote-age-minutes 不得覆盖场景冻结的新鲜度阈值")
    if minimum_recommendable_options != frozen_minimum_options:
        errors.append("--minimum-recommendable-options 不得覆盖场景冻结的可推荐方案下限")
    if errors:
        raise RuntimeError(
            "live-v4 Done-Gate threshold contract failed before search: " + "；".join(errors)
        )
    return frozen_maximum_age, frozen_minimum_options


def _api_payload(request: dict[str, Any]) -> dict[str, Any]:
    _validate_request_contract(request)
    candidate_set = system_stay_plan_candidate_set()
    return LiveFlexibleFromTextPlanningRequest.model_validate(
        {
            "requirement": request["requirement"],
            "calendars": request.get("calendars", []),
            "coverage_mode": request["coverage_mode"],
            "max_pairs": request["max_pairs"],
            "timeout_seconds": request["timeout_seconds"],
            "total_timeout_seconds": request["total_timeout_seconds"],
            "publication_refresh_minimum_options": (_FROZEN_MINIMUM_RECOMMENDABLE_OPTIONS),
            "stay_plan_candidate_set": candidate_set.model_dump(mode="json"),
        }
    ).model_dump(mode="json")


def _client_request_timeout_seconds(
    request: dict[str, Any],
    configured_timeout_seconds: float | None,
) -> float:
    """Keep async job polling alive beyond the frozen server execution budget."""

    server_timeout_seconds = float(request["total_timeout_seconds"])
    minimum_timeout_seconds = server_timeout_seconds + _MINIMUM_CLIENT_TIMEOUT_MARGIN_SECONDS
    if configured_timeout_seconds is None:
        return minimum_timeout_seconds
    if (
        not math.isfinite(configured_timeout_seconds)
        or configured_timeout_seconds < minimum_timeout_seconds
    ):
        raise RuntimeError(
            "--request-timeout-seconds 必须至少为服务端冻结执行预算 "
            f"{server_timeout_seconds:g}s + "
            f"{_MINIMUM_CLIENT_TIMEOUT_MARGIN_SECONDS:g}s，"
            "否则客户端可能先于结构化 504/取消证据超时"
        )
    return configured_timeout_seconds


def _job_idempotency_key(payload: dict[str, Any], attempt_id: str) -> str:
    """Bind retries within one fresh attempt to the canonical API payload."""

    if _ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None:
        raise ValueError("attempt_id must be 32 lowercase hexadecimal characters")
    return f"tripchord-live-v4-{_canonical_sha256(payload)}-{attempt_id}"


def _new_live_job_control(
    request: dict[str, Any],
    payload: dict[str, Any],
    *,
    client_wait_timeout_seconds: float,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    scenario_sha256 = _canonical_sha256(request)
    api_payload_sha256 = _canonical_sha256(payload)
    fresh_attempt_id = attempt_id or secrets.token_hex(16)
    return {
        "transport": "async_post_tenant_scoped_get_poll",
        "scenario_sha256": scenario_sha256,
        "api_payload_sha256": api_payload_sha256,
        "idempotency": {
            "key": _job_idempotency_key(payload, fresh_attempt_id),
            "attempt_id": fresh_attempt_id,
            "derivation": "sha256(canonical API payload) + fresh attempt_id",
            "credential_inputs": False,
        },
        "poll_interval_seconds": _JOB_POLL_INTERVAL_SECONDS,
        "client_wait_timeout_seconds": client_wait_timeout_seconds,
        "job_id": None,
        "replayed": None,
        "status_url": None,
        "events_url": None,
        "revision_history": [],
        "stage_progress_history": [],
        "terminal_job": None,
        "cancellation_receipt": None,
    }


def _job_revision_record(snapshot: LivePlanningJobSnapshot) -> dict[str, Any]:
    result_sha256 = _canonical_sha256(snapshot.result) if snapshot.result is not None else None
    ordered_checkpoint_sha256 = [
        checkpoint.checkpoint_sha256 for checkpoint in snapshot.pair_checkpoints
    ]
    return {
        "revision": snapshot.revision,
        "state": snapshot.state.value,
        "stage": snapshot.stage,
        "progress": snapshot.progress,
        "cancellation_requested": snapshot.cancellation_requested,
        "error": snapshot.error,
        "safe_failure_code": (
            snapshot.safe_failure_code.value if snapshot.safe_failure_code is not None else None
        ),
        "safe_failure_details_digest": snapshot.safe_failure_details_digest,
        "request_sha256": snapshot.request_sha256,
        "checkpoint_count": len(ordered_checkpoint_sha256),
        "ordered_checkpoint_sha256": ordered_checkpoint_sha256,
        "checkpoint_chain_sha256": _canonical_sha256(ordered_checkpoint_sha256),
        "model_trace_scope_sha256": getattr(snapshot, "model_trace_scope_sha256", None),
        "model_trace_count": getattr(snapshot, "model_trace_count", None),
        "model_trace_success_count": getattr(
            snapshot,
            "model_trace_success_count",
            None,
        ),
        "model_trace_failure_count": getattr(
            snapshot,
            "model_trace_failure_count",
            None,
        ),
        "result_sha256": result_sha256,
        "updated_at": snapshot.updated_at.isoformat(),
        "deadline_at": snapshot.deadline_at.isoformat(),
    }


def _job_snapshot_evidence(snapshot: LivePlanningJobSnapshot) -> dict[str, Any]:
    """Whitelist the terminal control-plane fields allowed into the evidence bundle."""

    return {
        "id": snapshot.id,
        "state": snapshot.state.value,
        "stage": snapshot.stage,
        "progress": snapshot.progress,
        "cancellation_requested": snapshot.cancellation_requested,
        "revision": snapshot.revision,
        "request_sha256": snapshot.request_sha256,
        "pair_checkpoints": [
            checkpoint.model_dump(mode="json") for checkpoint in snapshot.pair_checkpoints
        ],
        "model_trace_scope_sha256": getattr(
            snapshot,
            "model_trace_scope_sha256",
            None,
        ),
        "model_trace_count": getattr(snapshot, "model_trace_count", None),
        "model_trace_success_count": getattr(
            snapshot,
            "model_trace_success_count",
            None,
        ),
        "model_trace_failure_count": getattr(
            snapshot,
            "model_trace_failure_count",
            None,
        ),
        "result_sha256": (
            _canonical_sha256(snapshot.result) if snapshot.result is not None else None
        ),
        "error": snapshot.error,
        "safe_failure_code": (
            snapshot.safe_failure_code.value if snapshot.safe_failure_code is not None else None
        ),
        "safe_failure_details": (
            snapshot.safe_failure_details.model_dump(mode="json")
            if snapshot.safe_failure_details is not None
            else None
        ),
        "safe_failure_details_digest": snapshot.safe_failure_details_digest,
        "created_at": snapshot.created_at.isoformat(),
        "updated_at": snapshot.updated_at.isoformat(),
        "deadline_at": snapshot.deadline_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
    }


def _record_job_snapshot(
    control: dict[str, Any],
    snapshot: LivePlanningJobSnapshot,
    *,
    observed_at: datetime | None = None,
) -> None:
    """Record revision and stage/progress changes while rejecting state rollback."""

    expected_job_id = control.get("job_id")
    if expected_job_id is not None and snapshot.id != expected_job_id:
        raise RuntimeError("live-v4 tenant-scoped job GET returned a different job identity")
    if expected_job_id is None:
        control["job_id"] = snapshot.id

    expected_request_sha256 = TypeAdapter(str).validate_python(control.get("api_payload_sha256"))
    if snapshot.request_sha256 != expected_request_sha256:
        raise RuntimeError(
            "live-v4 async job request SHA is not bound to the canonical API payload"
        )

    revision_record = _job_revision_record(snapshot)
    revision_history = TypeAdapter(list[dict[str, Any]]).validate_python(
        control.get("revision_history", [])
    )
    if revision_history:
        previous = dict(revision_history[-1])
        previous.pop("observed_at", None)
        if snapshot.revision < int(previous["revision"]):
            raise RuntimeError("live-v4 async job revision moved backwards")
        previous_checkpoint_sha256 = TypeAdapter(list[str]).validate_python(
            previous.get("ordered_checkpoint_sha256", [])
        )
        current_checkpoint_sha256 = TypeAdapter(list[str]).validate_python(
            revision_record["ordered_checkpoint_sha256"]
        )
        if current_checkpoint_sha256[: len(previous_checkpoint_sha256)] != (
            previous_checkpoint_sha256
        ):
            raise RuntimeError(
                "live-v4 async job checkpoint chain did not grow by immutable prefix"
            )
        if snapshot.revision == int(previous["revision"]):
            if revision_record != previous:
                raise RuntimeError("live-v4 async job changed without increasing its revision")
            if snapshot.state in _TERMINAL_JOB_STATES:
                control["terminal_job"] = _job_snapshot_evidence(snapshot)
            return

    observed = (observed_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    revision_history.append({**revision_record, "observed_at": observed})
    control["revision_history"] = revision_history

    stage_progress_history = TypeAdapter(list[dict[str, Any]]).validate_python(
        control.get("stage_progress_history", [])
    )
    change_signature = (
        snapshot.state.value,
        snapshot.stage,
        snapshot.progress,
    )
    previous_signature = None
    if stage_progress_history:
        previous_change = stage_progress_history[-1]
        previous_signature = (
            previous_change["state"],
            previous_change["stage"],
            previous_change["progress"],
        )
    if change_signature != previous_signature:
        stage_progress_history.append(
            {
                "revision": snapshot.revision,
                "state": snapshot.state.value,
                "stage": snapshot.stage,
                "progress": snapshot.progress,
                "observed_at": observed,
            }
        )
        control["stage_progress_history"] = stage_progress_history

    if snapshot.state in _TERMINAL_JOB_STATES:
        control["terminal_job"] = _job_snapshot_evidence(snapshot)


async def _submit_flexible_live_job(
    client: httpx.AsyncClient,
    base: str,
    payload: dict[str, Any],
    control: dict[str, Any],
) -> StartLiveFlexibleFromTextJobResponse:
    idempotency = TypeAdapter(dict[str, Any]).validate_python(control["idempotency"])
    idempotency_key = TypeAdapter(str).validate_python(idempotency["key"])
    response = await client.post(
        f"{base}{_FROM_TEXT_JOBS_ENDPOINT}",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    response_payload = _safe_response_json(response, "live-v4 async job submission")
    if response.status_code != 202:
        raise RuntimeError(
            f"live-v4 async job submission must return HTTP 202; observed {response.status_code}"
        )
    started = StartLiveFlexibleFromTextJobResponse.model_validate(response_payload)
    control["job_id"] = started.job.id
    expected_status_url = f"{_FROM_TEXT_JOBS_ENDPOINT}/{started.job.id}"
    control["status_url"] = expected_status_url
    if started.status_url != expected_status_url:
        raise RuntimeError("live-v4 async job returned an unbound status_url")
    if started.events_url != f"{expected_status_url}/events":
        raise RuntimeError("live-v4 async job returned an unbound events_url")

    control.update(
        {
            "replayed": started.replayed,
            "events_url": started.events_url,
            "control_plane_boundary": started.boundary,
        }
    )
    _record_job_snapshot(control, started.job)
    return started


async def _cancel_flexible_live_job(
    client: httpx.AsyncClient,
    base: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort same-tenant cancellation without persisting arbitrary response data."""

    job_id = TypeAdapter(str).validate_python(control.get("job_id"))
    expected_status_url = f"{_FROM_TEXT_JOBS_ENDPOINT}/{job_id}"
    receipt: dict[str, Any] = {
        "attempted": True,
        "method": "DELETE",
        "job_id": job_id,
        "status_url": expected_status_url,
        "request_sha256": control.get("api_payload_sha256"),
        "outcome": "delete_failed",
        "status_code": None,
    }
    try:
        response = await asyncio.wait_for(
            client.delete(f"{base}{expected_status_url}"),
            timeout=_CANCELLATION_TIMEOUT_SECONDS,
        )
        receipt["status_code"] = response.status_code
        if response.status_code == 404:
            receipt["outcome"] = "not_found"
            return receipt
        if response.status_code != 200:
            receipt["outcome"] = "http_rejected"
            return receipt
        try:
            snapshot = LivePlanningJobSnapshot.model_validate(response.json())
        except (ValueError, json.JSONDecodeError):
            receipt["outcome"] = "invalid_response"
            return receipt
        if snapshot.id != job_id or snapshot.request_sha256 != control.get("api_payload_sha256"):
            receipt["outcome"] = "identity_mismatch"
            return receipt
        receipt.update(
            {
                "outcome": "acknowledged",
                "state": snapshot.state.value,
                "stage": snapshot.stage,
                "revision": snapshot.revision,
                "cancellation_requested": snapshot.cancellation_requested,
                "checkpoint_count": len(snapshot.pair_checkpoints),
            }
        )
        return receipt
    except Exception as exc:
        receipt["failure_type"] = type(exc).__name__
        return receipt


async def _cancel_after_runner_failure(
    args: argparse.Namespace,
    base: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    """Use the same tenant credentials to request cancellation after runner failure."""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_CANCELLATION_TIMEOUT_SECONDS),
            headers=_headers(args.api_token),
        ) as client:
            return await _cancel_flexible_live_job(client, base, control)
    except Exception as exc:
        return {
            "attempted": True,
            "method": "DELETE",
            "job_id": control.get("job_id"),
            "status_url": f"{_FROM_TEXT_JOBS_ENDPOINT}/{control.get('job_id')}",
            "request_sha256": control.get("api_payload_sha256"),
            "outcome": "delete_failed",
            "status_code": None,
            "failure_type": type(exc).__name__,
        }


async def _await_flexible_live_job(
    client: httpx.AsyncClient,
    base: str,
    control: dict[str, Any],
    *,
    client_wait_timeout_seconds: float,
    poll_interval_seconds: float = _JOB_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LivePlanningJobSnapshot:
    """Poll the tenant-scoped status endpoint and fail closed on every bad terminal."""

    if poll_interval_seconds <= 0 or not math.isfinite(poll_interval_seconds):
        raise ValueError("poll_interval_seconds must be finite and positive")
    job_id = TypeAdapter(str).validate_python(control.get("job_id"))
    status_url = TypeAdapter(str).validate_python(control.get("status_url"))
    expected_status_url = f"{_FROM_TEXT_JOBS_ENDPOINT}/{job_id}"
    if status_url != expected_status_url:
        raise RuntimeError("live-v4 async job status_url is not bound to its job id")

    clock = monotonic or asyncio.get_running_loop().time
    deadline = clock() + client_wait_timeout_seconds
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError(
                "live-v4 async job client wait budget expired before a terminal state"
            )
        try:
            response = await asyncio.wait_for(
                client.get(f"{base}{status_url}"),
                timeout=remaining,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise RuntimeError(
                "live-v4 async job client wait budget expired before a terminal state"
            ) from exc
        if response.status_code == 404:
            raise RuntimeError(
                "live-v4 tenant-scoped job GET returned 404; "
                "the job is absent, expired, or not visible to this tenant"
            )
        response_payload = _safe_response_json(response, "live-v4 async job status")
        if response.status_code != 200:
            raise RuntimeError(
                "live-v4 async job status GET must return HTTP 200; "
                f"observed {response.status_code}"
            )
        snapshot = LivePlanningJobSnapshot.model_validate(response_payload)
        _record_job_snapshot(control, snapshot)
        if snapshot.state == LivePlanningJobState.SUCCEEDED:
            if snapshot.result is None:
                raise RuntimeError("live-v4 async job succeeded without a complete terminal result")
            return snapshot
        if snapshot.state in {
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        }:
            safe_failure = (
                "; safe_failure_code="
                f"{snapshot.safe_failure_code.value}; "
                "safe_failure_details_digest="
                f"{snapshot.safe_failure_details_digest}"
                if snapshot.safe_failure_code is not None
                and snapshot.safe_failure_details_digest is not None
                else ""
            )
            raise RuntimeError(
                "live-v4 async job reached fail-closed terminal state "
                f"{snapshot.state.value}: stage={snapshot.stage}; "
                f"error={snapshot.error or 'not supplied'}{safe_failure}"
            )

        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError(
                "live-v4 async job client wait budget expired before a terminal state"
            )
        await sleep(min(poll_interval_seconds, remaining))


def _validate_terminal_pair_checkpoints(
    snapshot: LivePlanningJobSnapshot,
    flexible: FlexibleLiveAgentRun,
) -> dict[str, Any]:
    """Bind the three immutable checkpoints to the final ordered pair executions."""

    checkpoints = snapshot.pair_checkpoints
    pair_runs = tuple(flexible.pair_runs)
    if len(checkpoints) != 3:
        raise RuntimeError(
            "live-v4 succeeded job must expose exactly 3 pair checkpoints; "
            f"observed {len(checkpoints)}"
        )
    if len(pair_runs) != 3:
        raise RuntimeError(
            "live-v4 succeeded result must expose exactly 3 final pair runs; "
            f"observed {len(pair_runs)}"
        )
    bindings: list[dict[str, Any]] = []
    for checkpoint, execution in zip(checkpoints, pair_runs, strict=True):
        execution_task_ids = tuple(task.id for task in execution.query_tasks)
        mismatches: list[str] = []
        if checkpoint.date_pair_id != execution.date_pair.id:
            mismatches.append("date_pair_id")
        if checkpoint.departure_date != execution.date_pair.departure_date:
            mismatches.append("departure_date")
        if checkpoint.return_date != execution.date_pair.return_date:
            mismatches.append("return_date")
        if checkpoint.state.value != execution.state.value:
            mismatches.append("state")
        if checkpoint.query_task_ids != execution_task_ids:
            mismatches.append("query_task_ids")
        if mismatches:
            raise RuntimeError(
                "live-v4 terminal checkpoint does not match its final pair run at "
                f"sequence {checkpoint.sequence}: {', '.join(mismatches)}"
            )
        bindings.append(
            {
                "sequence": checkpoint.sequence,
                "date_pair_id": checkpoint.date_pair_id,
                "departure_date": checkpoint.departure_date.isoformat(),
                "return_date": checkpoint.return_date.isoformat(),
                "state": checkpoint.state.value,
                "query_task_ids": list(execution_task_ids),
                "query_task_ids_sha256": _canonical_sha256(list(execution_task_ids)),
                # C-122 round-19 (gap 4): the FULL business-summary fields, so
                # the layer-6 validator can independently recompute
                # ``run_summary_sha256`` from the binding's carried fields via
                # the checkpoint model's authoritative ``_run_summary`` digest —
                # a doctored summary can never pass with a copied digest.
                "run_purpose": checkpoint.run_purpose,
                "finalization_state": checkpoint.finalization_state,
                "decision_state": checkpoint.decision_state,
                "source_task_count": checkpoint.source_task_count,
                "exploration_seal_passed": checkpoint.exploration_seal_passed,
                "all_platforms_complete": checkpoint.all_platforms_complete,
                "failure_class": checkpoint.failure_class,
                "run_summary_sha256": checkpoint.run_summary_sha256,
                "captured_at": checkpoint.captured_at.isoformat(),
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
                "request_sha256": checkpoint.request_sha256,
            }
        )
    # C-122 supervision 18:13 (wrong request): every checkpoint must carry ONE
    # request identity bound to the terminal job's own request SHA — a foreign
    # request binding is a forged checkpoint chain even when the digests line up.
    request_sha256s = {checkpoint.request_sha256 for checkpoint in checkpoints}
    if len(request_sha256s) != 1:
        raise RuntimeError(
            "live-v4 terminal pair checkpoints do not share one request identity"
        )
    request_sha256 = next(iter(request_sha256s))
    if snapshot.request_sha256 is not None and request_sha256 != snapshot.request_sha256:
        raise RuntimeError(
            "live-v4 terminal pair checkpoint request SHA is not bound to the "
            "terminal job's request identity"
        )
    return {
        "passed": True,
        "count": len(bindings),
        "ordered_checkpoint_sha256": [checkpoint.checkpoint_sha256 for checkpoint in checkpoints],
        "checkpoint_chain_sha256": _canonical_sha256(
            [checkpoint.checkpoint_sha256 for checkpoint in checkpoints]
        ),
        "request_sha256": request_sha256,
        "bindings": bindings,
    }


def _selected_option(
    flexible: FlexibleLiveAgentRun,
) -> tuple[str, str, LivePackageAgentRun, LivePackageAgentRun]:
    if not flexible.recommended_option_ids:
        raise RuntimeError(
            "flexible live-v4 run did not produce a recommendable date/stay-plan option"
        )
    selected_option_id = flexible.recommended_option_ids[0]
    ranked = next(
        (item for item in flexible.ranked_options if item.option_id == selected_option_id),
        None,
    )
    if ranked is None or ranked.stay_plan_id is None:
        raise RuntimeError("recommended live-v4 option has no frozen stay-plan identity")
    matching_executions = tuple(
        item
        for item in flexible.pair_runs
        if item.date_pair.id == ranked.date_pair_id and item.run is not None
    )
    if len(matching_executions) != 1:
        raise RuntimeError("recommended live-v4 option has no completed date-pair run")
    execution = matching_executions[0]
    publication = execution.run
    exploration = execution.exploration_run
    audit = execution.publication_refresh_audit
    if publication is None or exploration is None:
        raise RuntimeError(
            "recommended live-v4 option must bind separate exploration and publication runs"
        )
    if publication.evidence_scope != LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH:
        raise RuntimeError(
            "recommended live-v4 option is not backed by publication component refresh"
        )
    if exploration.evidence_scope != LiveEvidenceScope.FULL_SEARCH:
        raise RuntimeError("recommended live-v4 option has no full-search exploration evidence")
    if (
        audit is None
        or not audit.binding_passed
        or audit.refreshed_option_id != selected_option_id
        or tuple(audit.source_task_ids) != tuple(publication.source_task_ids)
        or publication.package is None
        or audit.refreshed_candidate_id != publication.package.final_candidate.id
        or exploration.package is None
        or audit.previous_candidate_id != exploration.package.final_candidate.id
    ):
        raise RuntimeError("recommended live-v4 option failed publication refresh audit binding")
    if publication.selected_stay_plan_id != ranked.stay_plan_id:
        raise RuntimeError("recommended option identity differs from the selected frozen stay plan")
    return selected_option_id, ranked.date_pair_id, exploration, publication


def _event_target(initial: LivePackageAgentRun) -> tuple[str, str]:
    if initial.package is None:
        raise RuntimeError("selected live-v4 option has no accepted package")
    candidate = initial.package.final_candidate
    if not candidate.lodgings:
        raise RuntimeError("selected live-v4 package has no lodging component to perturb")
    target = max(candidate.lodgings, key=lambda item: (item.night_count, item.id))
    return target.id, target.provider


def _synthetic_sold_out_event_body(
    target_component_id: str,
    affected_provider: str,
    *,
    injected_at: datetime | None = None,
) -> dict[str, Any]:
    """Build an explicitly hypothetical fault; this is not a supplier fact."""

    occurred_at = (injected_at or datetime.now(UTC)).astimezone(UTC)
    return {
        "event": {
            "id": (f"live-v4-gate-synthetic-sold-out-{occurred_at.strftime('%Y%m%d%H%M%S%f')}"),
            "kind": PackageEventKind.SOLD_OUT.value,
            "target_component_id": target_component_id,
            "affected_provider": affected_provider,
            "occurred_at": occurred_at.isoformat(),
            "source": _SYNTHETIC_DONE_GATE_EVENT_SOURCE,
        }
    }


def _synthetic_fault_contract() -> dict[str, Any]:
    return {
        "mode": "synthetic_sold_out_fault_injection",
        "source": _SYNTHETIC_DONE_GATE_EVENT_SOURCE,
        "platform_sold_out_observed": False,
        "platform_price_change_observed": False,
        "verified_change_scope": ("different_available_replacement_identity_not_platform_sold_out"),
        "claim_boundary": _SYNTHETIC_FAULT_CLAIM_BOUNDARY,
    }


def _validate_synthetic_sold_out_replan(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    *,
    target_component_id: str,
    affected_provider: str,
) -> dict[str, Any]:
    """Fail closed unless the hypothetical fault caused a verified local repair."""

    errors: list[str] = []
    live_event = event.event
    if live_event.kind != PackageEventKind.SOLD_OUT:
        errors.append("事件类型不是 synthetic sold_out")
    if live_event.source != _SYNTHETIC_DONE_GATE_EVENT_SOURCE:
        errors.append("事件 source 未标明 Done-Gate synthetic fault injection")
    if live_event.target_component_id != target_component_id:
        errors.append("事件目标与注入前选定住宿不一致")
    if live_event.affected_provider.value != affected_provider:
        errors.append("事件 provider 与目标住宿 provider 不一致")
    if event.requeried_providers != (live_event.affected_provider,):
        errors.append("事件没有只读重查且只重查同一个 affected_provider")
    if len(event.source_task_ids) != 1:
        errors.append("事件必须且只能生成一个同 provider Source task")
    if event.global_run is not None:
        errors.append("synthetic sold_out 不得以全局重规划冒充局部 Repair")

    resolution = event.event_resolution
    replacement_component_id: str | None = None
    stable_product_key_before: str | None = None
    stable_product_key_after: str | None = None
    if resolution is None:
        errors.append("事件缺少 resolve_offer_event 解析证据")
    else:
        replacement_component_id = resolution.replacement_component_id
        semantic_diff = resolution.semantic_diff
        envelope = resolution.envelope
        new_value = envelope.new_value
        stable_product_key_before = envelope.old_value.stable_product_key
        stable_product_key_after = new_value.stable_product_key if new_value is not None else None
        if resolution.disposition != EventDisposition.LOCAL_REPAIR:
            errors.append("resolve_offer_event 未裁定 local_repair")
        if event.applied_disposition != EventDisposition.LOCAL_REPAIR:
            errors.append("模型或主流程未保持 local_repair 处置")
        if resolution.verified_change is not True:
            errors.append("resolve_offer_event 未验证替代商品语义变化")
        if resolution.candidate_pool_expansion_required:
            errors.append("局部替代不应要求扩大候选池")
        if (
            envelope.kind != PackageEventKind.SOLD_OUT
            or envelope.source != _SYNTHETIC_DONE_GATE_EVENT_SOURCE
            or envelope.target_component_id != target_component_id
            or envelope.old_value.transient_offer_id != target_component_id
            or envelope.old_value.provider != affected_provider
            or envelope.old_value.availability != QuoteAvailability.AVAILABLE.value
        ):
            errors.append("事件 envelope 未绑定 synthetic source 与原目标商品")
        if (
            new_value is None
            or replacement_component_id != new_value.transient_offer_id
            or replacement_component_id == target_component_id
            or new_value.provider != affected_provider
            or new_value.availability != QuoteAvailability.AVAILABLE.value
        ):
            errors.append("事件没有绑定同 provider 的不同可用替代商品")
        if (
            new_value is None
            or stable_product_key_before is None
            or stable_product_key_after is None
            or stable_product_key_before == stable_product_key_after
            or envelope.old_value.identity_ambiguous
            or new_value.identity_ambiguous
            or envelope.old_value.product_identity_confidence == OfferIdentityConfidence.LOW
            or new_value.product_identity_confidence == OfferIdentityConfidence.LOW
        ):
            errors.append("原商品与替代商品没有可区分的稳定商品身份")
        if (
            semantic_diff is None
            or semantic_diff.change != OfferSemanticChange.DIFFERENT_PRODUCT
            or not semantic_diff.different_product_confirmed
            or semantic_diff.same_product
            or semantic_diff.same_offer
            or semantic_diff.identity_ambiguous
            or semantic_diff.price_changed
        ):
            errors.append("semantic_offer_diff 未确认不同商品或错误声称价格变化")

    previous = initial.package
    package = event.package
    if previous is None or package is None:
        errors.append("事件前后缺少可审计整包")
    else:
        initial_target_matches = tuple(
            item for item in previous.final_candidate.lodgings if item.id == target_component_id
        )
        if (
            len(initial_target_matches) != 1
            or initial_target_matches[0].provider != affected_provider
            or initial_target_matches[0].availability != QuoteAvailability.AVAILABLE
        ):
            errors.append("synthetic fault 未唯一绑定事件前同 provider 的可用住宿")
        diff = package.diff
        handoff = package.event_handoff
        replacement_matches = tuple(
            item for item in package.final_candidate.lodgings if item.id == replacement_component_id
        )
        if (
            diff is None
            or diff.removed_component_ids != (target_component_id,)
            or diff.added_component_ids != (replacement_component_id,)
            or diff.changed_component_ids
        ):
            errors.append("Repair diff 不是精确删除 1 个目标并新增 1 个替代")
        if any(item.id == target_component_id for item in package.final_candidate.lodgings):
            errors.append("Repair 后整包仍包含 synthetic sold_out 目标")
        if (
            len(replacement_matches) != 1
            or replacement_matches[0].provider != affected_provider
            or replacement_matches[0].availability != QuoteAvailability.AVAILABLE
        ):
            errors.append("Repair 后整包未唯一包含同 provider 的可用替代住宿")
        if handoff is None:
            errors.append("事件整包缺少 Repair 到 Event ReVerifier 交接")
        else:
            repair_event = handoff.repair.event
            reverification = handoff.reverification
            if (
                repair_event.id != live_event.id
                or repair_event.kind != PackageEventKind.SOLD_OUT
                or repair_event.target_component_id != target_component_id
                or repair_event.replacement_component_id != replacement_component_id
            ):
                errors.append("Repair handoff 未精确绑定 synthetic sold_out 替换")
            if (
                reverification is None
                or reverification.phase != PackageVerificationPhase.EVENT_REVERIFICATION
                or not reverification.matches(package.final_candidate)
                or reverification.errors
            ):
                errors.append("Event ReVerifier 未对 Repair 最终候选完成无硬错误复验")
        audit = event.package_reverification_audit
        if (
            audit is None
            or not audit.passed
            or audit.before_candidate_id != previous.final_candidate.id
            or audit.after_candidate_id != package.final_candidate.id
        ):
            errors.append("异构独立审计未通过或未绑定 Repair 前后候选")
        if (
            event.decision.state != PackageDecisionState.ACCEPT
            or package.final_decision.state != PackageDecisionState.ACCEPT
        ):
            errors.append("主控没有接受 Event ReVerifier 与独立审计后的 Repair")

    if errors:
        raise RuntimeError(
            "synthetic sold_out Done-Gate event contract failed: " + "；".join(errors)
        )
    assert resolution is not None
    assert replacement_component_id is not None
    return {
        "passed": True,
        "source": _SYNTHETIC_DONE_GATE_EVENT_SOURCE,
        "platform_sold_out_observed": False,
        "same_provider_requery": True,
        "source_task_count": len(event.source_task_ids),
        "stable_different_product_confirmed": True,
        "verified_change_scope": ("different_available_replacement_identity_not_platform_sold_out"),
        "stable_product_key_before": stable_product_key_before,
        "stable_product_key_after": stable_product_key_after,
        "replacement_component_id": replacement_component_id,
        "repair_removed_component_count": 1,
        "repair_added_component_count": 1,
        "event_reverification_passed": True,
        "independent_audit_passed": True,
        "master_accepted": True,
        "claim_boundary": _SYNTHETIC_FAULT_CLAIM_BOUNDARY,
    }


async def _request_event_replan(
    client: httpx.AsyncClient,
    base: str,
    run_id: str,
    event_body: dict[str, Any],
) -> LiveEventReplanRun:
    response = await client.post(
        f"{base}/api/v1/agents/live-plans/{run_id}/events/replan",
        json=event_body,
    )
    payload = _safe_response_json(response, "live-v4 event replan")
    if payload.get("run") is None:
        raise RuntimeError("live-v4 event replan response did not contain a run")
    return LiveEventReplanRun.model_validate(payload["run"])


async def _run(
    args: argparse.Namespace,
    *,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
    now_factory: Callable[[], datetime] = _utc_now,
) -> int:
    request: dict[str, Any] | None = None
    publication_run: LivePackageAgentRun | None = None
    event: LiveEventReplanRun | None = None
    stage = "load_request"
    context: dict[str, Any] = {}
    base = args.api_base.rstrip("/")
    # Capture the repo revision BEFORE any work: every evidence bundle is later
    # checked against this marker, so a HEAD move or tracked-tree change during
    # the run fails the evidence closed instead of naming a stale revision.
    start_revision = _repo_revision()
    context["start_revision"] = {
        key: start_revision.get(key)
        for key in ("toplevel", "branch", "commit_sha", "worktree_dirty")
    }
    try:
        request = _load_request(args.request)
        stage = "validate_frozen_request"
        (
            maximum_quote_age_minutes,
            minimum_recommendable_options,
        ) = _frozen_done_gate_thresholds(
            request,
            maximum_quote_age_minutes=args.maximum_quote_age_minutes,
            minimum_recommendable_options=args.minimum_recommendable_options,
        )
        payload = _api_payload(request)
        scenario_sha256 = _canonical_sha256(request)
        api_payload_sha256 = _canonical_sha256(payload)
        context["request_identity"] = {
            "scenario_sha256": scenario_sha256,
            "api_payload_sha256": api_payload_sha256,
            "digests_are_distinct_contracts": True,
        }
        stage = "validate_client_timeout_contract"
        request_timeout_seconds = _client_request_timeout_seconds(
            request,
            args.request_timeout_seconds,
        )
        context["timeout_contract"] = {
            "server_execution_timeout_seconds": request["total_timeout_seconds"],
            "client_wait_timeout_seconds": request_timeout_seconds,
            "minimum_client_margin_seconds": (_MINIMUM_CLIENT_TIMEOUT_MARGIN_SECONDS),
        }
        require_model_enhancement = bool(getattr(args, "require_model_enhancement", False))
        context["runner_contract"] = {
            "require_model_enhancement": require_model_enhancement,
            "maximum_quote_age_minutes": maximum_quote_age_minutes,
            "minimum_recommendable_options": minimum_recommendable_options,
        }
        context["event_injection_contract"] = _synthetic_fault_contract()
        candidate_set = system_stay_plan_candidate_set()
        context["api_payload_candidate_set_sha256"] = candidate_set.candidate_set_sha256
        async with (client_factory or httpx.AsyncClient)(
            timeout=httpx.Timeout(request_timeout_seconds),
            headers=_headers(args.api_token),
        ) as client:
            stage = "runtime_preflight"
            runtime_before = await _runtime_evidence(
                client,
                base,
                label="live-v4 runtime preflight",
            )
            context["runtime_before_run"] = runtime_before
            stage = "validate_runtime_timeout_contract"
            _validate_runtime_timeout_contract(runtime_before)
            stage = "validate_required_model_runtime"
            _validate_required_model_runtime(
                runtime_before,
                require_model_enhancement=require_model_enhancement,
            )
            stage = "validate_runtime_provenance"
            _validate_runtime_provenance(runtime_before)
            stage = "companion_preflight"
            companion = await _preflight_companion(
                client,
                base,
                args.bridge_token,
            )
            context["companion_preflight"] = companion
            live_job_control = _new_live_job_control(
                request,
                payload,
                client_wait_timeout_seconds=request_timeout_seconds,
            )
            context["live_job_control"] = live_job_control
            stage = "submit_flexible_live_job"
            await _submit_flexible_live_job(
                client,
                base,
                payload,
                live_job_control,
            )
            stage = "await_flexible_live_job"
            terminal_job = await _await_flexible_live_job(
                client,
                base,
                live_job_control,
                client_wait_timeout_seconds=request_timeout_seconds,
            )
            stage = "validate_flexible_live_job_result"
            response_payload = TypeAdapter(dict[str, Any]).validate_python(terminal_job.result)
            context["interpretation"] = response_payload.get("interpretation")
            context["execution_boundary"] = response_payload.get("execution_boundary")
            context["model_enhancement_enabled"] = response_payload.get("model_enhancement_enabled")
            context["cached_pair_runs"] = response_payload.get(
                "cached_pair_runs",
                [],
            )
            expected_model_enhancement = require_model_enhancement
            if response_payload.get("model_enhancement_enabled") is not expected_model_enhancement:
                expected = "enabled" if expected_model_enhancement else "disabled"
                raise RuntimeError("live-v4 gate expected model enhancement to be " + expected)
            if response_payload.get("run") is None:
                raise RuntimeError("live-v4 response did not execute the real flexible plan")
            run = FlexibleLiveAgentRun.model_validate(response_payload["run"])
            stage = "validate_terminal_pair_checkpoints"
            context["pair_checkpoint_binding"] = _validate_terminal_pair_checkpoints(
                terminal_job,
                run,
            )
            stage = "validate_job_bound_model_trace_receipt"
            context["model_trace_receipt"] = _validate_model_trace_receipt(
                terminal_job,
                response_payload,
                api_payload_sha256=api_payload_sha256,
                require_model_enhancement=require_model_enhancement,
            )
            context["flexible_run"] = run.model_dump(mode="json")
            stage = "runtime_postflight"
            runtime_after = await _runtime_evidence(
                client,
                base,
                label="live-v4 runtime postflight",
            )
            context["runtime_after_run"] = runtime_after
            before_trace_count = TypeAdapter(int).validate_python(
                runtime_before["model_trace_count"]
            )
            after_trace_count = TypeAdapter(int).validate_python(runtime_after["model_trace_count"])
            model_trace_count_delta = after_trace_count - before_trace_count
            context["process_global_model_trace_diagnostic"] = {
                "authoritative": False,
                "before_count": before_trace_count,
                "after_count": after_trace_count,
                "delta": model_trace_count_delta,
                "boundary": (
                    "进程全局计数只用于诊断，可能受并发和环形缓冲区影响；"
                    "Done-Gate 只信任 terminal job 绑定的模型调用回执。"
                ),
            }
            if not run.recommended_option_ids:
                context["event_execution"] = {
                    "status": "skipped",
                    "skipped_reason": "no_recommendable_published_option",
                    "synthetic_event_injected": False,
                }
            else:
                stage = "select_recommendable_option"
                (
                    selected_option_id,
                    selected_pair_id,
                    exploration_run,
                    publication_run,
                ) = _selected_option(run)
                handles = TypeAdapter(list[dict[str, Any]]).validate_python(
                    response_payload.get("cached_pair_runs", [])
                )
                run_id = next(
                    (
                        str(item["run_id"])
                        for item in handles
                        if item.get("date_pair_id") == selected_pair_id
                    ),
                    None,
                )
                if run_id is None:
                    raise RuntimeError(
                        "selected live-v4 date pair was not cached for event replanning"
                    )
                context["selected_option_id"] = selected_option_id
                context["selected_pair_id"] = selected_pair_id
                context["selected_run_id"] = run_id
                context["selected_evidence_scope_binding"] = {
                    "exploration": LiveEvidenceScope.FULL_SEARCH.value,
                    "publication": (LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH.value),
                }
                context["selected_exploration_run"] = exploration_run.model_dump(mode="json")
                context["selected_publication_run"] = publication_run.model_dump(mode="json")
                # Retain the old key only as an explicitly documented compatibility
                # alias. It always points to the publication-time decision run.
                context["initial_run"] = context["selected_publication_run"]
                stage = "event_replan"
                target_id, provider = _event_target(publication_run)
                event_body = _synthetic_sold_out_event_body(
                    target_id,
                    provider,
                    injected_at=now_factory(),
                )
                context["injected_event"] = event_body
                context["event_execution"] = {
                    "status": "injected_pending_validation",
                    "skipped_reason": None,
                    "synthetic_event_injected": True,
                }
                event = await _request_event_replan(
                    client,
                    base,
                    run_id,
                    event_body,
                )
                context["event_run"] = event.model_dump(mode="json")
                stage = "validate_synthetic_fault_replan"
                context["synthetic_fault_validation"] = _validate_synthetic_sold_out_replan(
                    publication_run,
                    event,
                    target_component_id=target_id,
                    affected_provider=provider,
                )
                context["event_execution"]["status"] = "validated"
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        failed_job_control = context.get("live_job_control")
        if isinstance(failed_job_control, dict) and failed_job_control.get("job_id"):
            cancellation_receipt = await _cancel_after_runner_failure(
                args,
                base,
                failed_job_control,
            )
            failed_job_control["cancellation_receipt"] = _redact_explicit_secrets(
                cancellation_receipt,
                _runner_secrets(args),
            )
        captured_at = now_factory()
        bundle = _failure_evidence_bundle(
            request=request,
            stage=stage,
            error=exc,
            captured_at=captured_at,
            context=context,
            repo_revision=_repo_revision(start_revision),
        )
        bundle = TypeAdapter(dict[str, Any]).validate_python(
            _redact_explicit_secrets(bundle, _runner_secrets(args))
        )
        output = _write_evidence_bundle(
            args.output,
            bundle,
            passed=False,
            captured_at=captured_at,
        )
        print(
            json.dumps(
                {
                    "passed": False,
                    "run_status": "failed_before_done_gate",
                    "stage": stage,
                    "output": str(output),
                    "error": _redact_explicit_secrets(
                        str(exc),
                        _runner_secrets(args),
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    stage = "evaluate_done_gate"
    captured_at = now_factory()
    assert request is not None
    try:
        report = evaluate_live_v4_done_gate(
            run,
            expected_candidate_set=candidate_set,
            selected_initial=publication_run,
            event=event,
            evaluated_at=captured_at,
            maximum_quote_age_minutes=maximum_quote_age_minutes,
            minimum_recommendable_options=minimum_recommendable_options,
            minimum_exact_providers_per_selected_segment=int(
                request["stay_plan_profile"]["minimum_exact_providers_per_selected_segment"]
            ),
        )
    except (RuntimeError, ValueError, OSError) as exc:
        failed_job_control = context.get("live_job_control")
        if isinstance(failed_job_control, dict) and failed_job_control.get("job_id"):
            cancellation_receipt = await _cancel_after_runner_failure(
                args,
                base,
                failed_job_control,
            )
            failed_job_control["cancellation_receipt"] = _redact_explicit_secrets(
                cancellation_receipt,
                _runner_secrets(args),
            )
        bundle = _failure_evidence_bundle(
            request=request,
            stage=stage,
            error=exc,
            captured_at=captured_at,
            context=context,
            repo_revision=_repo_revision(start_revision),
        )
        bundle = TypeAdapter(dict[str, Any]).validate_python(
            _redact_explicit_secrets(bundle, _runner_secrets(args))
        )
        output = _write_evidence_bundle(
            args.output,
            bundle,
            passed=False,
            captured_at=captured_at,
        )
        print(
            json.dumps(
                {
                    "passed": False,
                    "run_status": "failed_before_done_gate",
                    "stage": stage,
                    "output": str(output),
                    "error": _redact_explicit_secrets(
                        str(exc),
                        _runner_secrets(args),
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    # The run is about to be certified: the repository it exercised must still
    # be the revision it started on.  A HEAD move or tracked-tree change during
    # the run means the evidence cannot name what actually ran — fail closed.
    repo_revision = _repo_revision(start_revision)
    if repo_revision.get("revision_changed_during_run"):
        raise RuntimeError(
            "repository revision changed during the run (TOCTOU): "
            f"start {repo_revision.get('start_revision')} != "
            f"end {repo_revision.get('commit_sha')} worktree_dirty="
            f"{repo_revision.get('worktree_dirty')}; evidence cannot name a "
            "tested revision"
        )
    bundle = _completed_evidence_bundle(
        request=request,
        report=report,
        captured_at=captured_at,
        context=context,
        repo_revision=repo_revision,
    )
    bundle = TypeAdapter(dict[str, Any]).validate_python(
        _redact_explicit_secrets(bundle, _runner_secrets(args))
    )
    output = _write_evidence_bundle(
        args.output,
        bundle,
        passed=report.passed,
        captured_at=captured_at,
    )
    print(
        json.dumps(
            {
                "passed": report.passed,
                "run_status": bundle["run_status"],
                "output": str(output),
                "candidate_set_sha256": candidate_set.candidate_set_sha256,
                "failed_checks": [item.name for item in report.checks if not item.passed],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed else 2


def main() -> None:
    try:
        code = asyncio.run(_run(_arguments()))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
