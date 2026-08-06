"""Build a typed :class:`SearchRun` from a completed live package run (v0.3).

The live system executes a DAG whose source tasks each end in a typed terminal
state.  This builder reduces the run's scheduler results and coverage back into
the canonical :class:`SourceAttempt` / :class:`TerminalReceipt` records so a
SearchRun can be persisted and recovered without replaying the DAG.

Terminal-state mapping is deliberately conservative:

- a browser task that succeeded with a usable quote contributes ``quote_found``;
- a succeeded task without a usable quote contributes ``bounded_no_exact_quote``;
- typed browser failures map to their canonical terminal state; unknown failure
  codes become ``provider_error``;
- iCom transfer tasks contribute ``quote_found`` only when usable options were
  produced.

None of ``login_required`` / ``captcha_required`` / ``dom_drift`` /
``timed_out`` / ``cancelled`` / ``provider_error`` is ever upgraded to a quote.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from tripchord.agents.live_system import LivePackageAgentRun, PlatformSearchCoverage
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.terminal import (
    SearchRun,
    SourceAttempt,
    SourceAttemptStatus,
    SourceTerminalState,
    TerminalReceipt,
)
from tripchord.providers.browser_bridge import (
    BrowserFailureCode,
    BrowserTaskState,
)

_RUN_ID_PREFIX = "search-run"


class SearchRunPersister(Protocol):
    """Async persistence hook for a completed :class:`SearchRun`."""

    async def __call__(
        self,
        tenant_id: str,
        run: LivePackageAgentRun,
    ) -> SearchRun: ...


SearchRunPersisterCallable = Callable[[str, LivePackageAgentRun], Awaitable[SearchRun]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def derive_scope_from_task_id(task_id: str) -> ProviderScopeKey | None:
    """Map a source task id to its frozen scope, or None when unmappable."""
    if task_id.startswith("public-transfer-icom-"):
        return ProviderScopeKey(provider="icom", vertical=ProviderVertical.TRANSFER)
    if not task_id.startswith("source-"):
        return None
    remainder = task_id[len("source-") :]
    provider, _, rest = remainder.partition("-")
    if not provider:
        return None
    if rest == "flight":
        return ProviderScopeKey(provider=provider, vertical=ProviderVertical.FLIGHT)
    if rest.startswith("lodging"):
        return ProviderScopeKey(provider=provider, vertical=ProviderVertical.LODGING)
    return None


def _browser_terminal_state(
    *,
    state: BrowserTaskState,
    failure_code: BrowserFailureCode | None,
    has_usable_quote: bool,
) -> SourceTerminalState:
    if state is BrowserTaskState.SUCCEEDED:
        return (
            SourceTerminalState.QUOTE_FOUND
            if has_usable_quote
            else SourceTerminalState.BOUNDED_NO_EXACT_QUOTE
        )
    if state is BrowserTaskState.CANCELLED:
        return SourceTerminalState.CANCELLED
    if failure_code is not None:
        mapping: dict[BrowserFailureCode, SourceTerminalState] = {
            BrowserFailureCode.LOGIN_REQUIRED: SourceTerminalState.LOGIN_REQUIRED,
            BrowserFailureCode.CAPTCHA_REQUIRED: SourceTerminalState.CAPTCHA_REQUIRED,
            BrowserFailureCode.DOM_DRIFT: SourceTerminalState.DOM_DRIFT,
            BrowserFailureCode.TIMEOUT: SourceTerminalState.TIMED_OUT,
            BrowserFailureCode.NO_INVENTORY: SourceTerminalState.CONFIRMED_EMPTY,
        }
        return mapping.get(failure_code, SourceTerminalState.PROVIDER_ERROR)
    return SourceTerminalState.PROVIDER_ERROR


def _task_failure_code(output: dict[str, JsonValue]) -> BrowserFailureCode | None:
    raw_snapshot = output.get("snapshot")
    if not isinstance(raw_snapshot, dict):
        return None
    raw_failure = raw_snapshot.get("failure")
    if not isinstance(raw_failure, dict):
        return None
    raw_code = raw_failure.get("code")
    if not isinstance(raw_code, str):
        return None
    try:
        return BrowserFailureCode(raw_code)
    except ValueError:
        return None


def _task_browser_state(output: dict[str, JsonValue]) -> BrowserTaskState | None:
    raw_snapshot = output.get("snapshot")
    if not isinstance(raw_snapshot, dict):
        return None
    raw_state = raw_snapshot.get("state")
    if not isinstance(raw_state, str):
        return None
    try:
        return BrowserTaskState(raw_state)
    except ValueError:
        return None


def _usable_quote_source_ids(coverage: tuple[PlatformSearchCoverage, ...]) -> set[str]:
    usable: set[str] = set()
    for item in coverage:
        usable.update(item.usable_quote_source_ids or item.successful_source_ids or ())
    return usable


def _failure_class_for_source(
    output: dict[str, JsonValue],
    state: SourceTerminalState,
) -> str | None:
    if state is SourceTerminalState.CANCELLED:
        return "cancelled"
    if state is SourceTerminalState.TIMED_OUT:
        return "timeout"
    raw_snapshot = output.get("snapshot")
    raw_failure = raw_snapshot.get("failure") if isinstance(raw_snapshot, dict) else None
    if isinstance(raw_failure, dict):
        code = raw_failure.get("code")
        if isinstance(code, str):
            return code
    if state is SourceTerminalState.PROVIDER_ERROR:
        return "provider_error"
    return None


def build_search_run(
    *,
    run: LivePackageAgentRun,
    run_id: str | None = None,
    snapshot_sha256: str | None = None,
    created_at: datetime | None = None,
) -> SearchRun:
    """Reduce one completed live run into a typed :class:`SearchRun`.

    ``run_id`` defaults to a deterministic hash of the frozen source task set so
    the same execution is recoverable idempotently.  ``snapshot_sha256``
    defaults to a hash over the frozen scopes actually executed; callers that
    froze a real :class:`SelectionSnapshot` must pass its real SHA instead.
    """
    resolved_run_id = run_id or _deterministic_run_id(run)
    resolved_snapshot = snapshot_sha256 or _scope_derived_snapshot_sha(run)
    created = created_at or _utc_now()
    result_by_task = {result.task_id: result for result in run.scheduler.results}
    usable_quotes = _usable_quote_source_ids(run.coverage)
    source_ids = (
        *run.source_task_ids,
        *run.public_transfer_task_ids,
    )
    attempts: list[SourceAttempt] = []
    receipts: list[TerminalReceipt] = []
    for task_id in dict.fromkeys(source_ids):
        scope = derive_scope_from_task_id(task_id)
        if scope is None:
            continue
        task_result = result_by_task.get(task_id)
        if task_result is None:
            attempts.append(
                SourceAttempt(
                    attempt_id=task_id,
                    run_id=resolved_run_id,
                    scope=scope,
                    status=SourceAttemptStatus.RUNNING,
                    generation=0,
                )
            )
            continue
        output = dict(task_result.output)
        browser_state = _task_browser_state(output)
        failure_code = _task_failure_code(output)
        has_usable_quote = task_id in usable_quotes
        if browser_state is not None:
            terminal_state = _browser_terminal_state(
                state=browser_state,
                failure_code=failure_code,
                has_usable_quote=has_usable_quote,
            )
        elif task_id.startswith("public-transfer-icom-"):
            raw_result = output.get("result")
            if task_result.success and isinstance(raw_result, dict):
                options = raw_result.get("options")
                terminal_state = (
                    SourceTerminalState.QUOTE_FOUND
                    if isinstance(options, (list, tuple)) and options
                    else SourceTerminalState.BOUNDED_NO_EXACT_QUOTE
                )
            else:
                terminal_state = SourceTerminalState.PROVIDER_ERROR
        elif task_result.success:
            terminal_state = (
                SourceTerminalState.QUOTE_FOUND
                if has_usable_quote
                else SourceTerminalState.BOUNDED_NO_EXACT_QUOTE
            )
        else:
            failure_class = task_result.failure_class or "provider_error"
            terminal_state = _failure_state_from_class(failure_class)
        terminal_at = created
        attempts.append(
            SourceAttempt(
                attempt_id=task_id,
                run_id=resolved_run_id,
                scope=scope,
                status=SourceAttemptStatus.TERMINAL,
                terminal_state=terminal_state,
                started_at=created,
                terminal_at=terminal_at,
                generation=0,
                failure_class=_failure_class_for_source(output, terminal_state),
                detail=None,
            )
        )
        receipts.append(
            TerminalReceipt(
                run_id=resolved_run_id,
                attempt_id=task_id,
                scope=scope,
                terminal_state=terminal_state,
                terminal_at=terminal_at,
                generation=0,
                evidence_sha256=None,
            )
        )
    return SearchRun(
        run_id=resolved_run_id,
        created_at=created,
        snapshot_sha256=resolved_snapshot,
        attempts=tuple(attempts),
    )


def _failure_state_from_class(failure_class: str) -> SourceTerminalState:
    normalized = failure_class.lower()
    if "login" in normalized:
        return SourceTerminalState.LOGIN_REQUIRED
    if "captcha" in normalized:
        return SourceTerminalState.CAPTCHA_REQUIRED
    if "dom_drift" in normalized or "drift" in normalized:
        return SourceTerminalState.DOM_DRIFT
    if "timeout" in normalized or "timed_out" in normalized:
        return SourceTerminalState.TIMED_OUT
    if "cancel" in normalized:
        return SourceTerminalState.CANCELLED
    return SourceTerminalState.PROVIDER_ERROR


def _scope_derived_snapshot_sha(run: LivePackageAgentRun) -> str:
    scopes = sorted(
        {
            scope
            for task_id in (*run.source_task_ids, *run.public_transfer_task_ids)
            if (scope := derive_scope_from_task_id(task_id)) is not None
        },
        key=lambda scope: scope.key,
    )
    canonical = {
        "schema": "tripchord-search-run-scope-snapshot-v1",
        "scopes": [scope.key for scope in scopes],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _deterministic_run_id(run: LivePackageAgentRun) -> str:
    canonical = {
        "task_ids": sorted((*run.source_task_ids, *run.public_transfer_task_ids)),
        "query": run.search_query.model_dump(mode="json"),
        "mode": run.mode.value,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"{_RUN_ID_PREFIX}-{digest[:48]}"
