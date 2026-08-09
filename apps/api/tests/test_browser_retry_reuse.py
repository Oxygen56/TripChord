from __future__ import annotations

from datetime import UTC, datetime

from tripchord.agents.live_system import (
    _should_reuse_lodging_result_tab,
    _with_reuse_lodging_result_tab,
)
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
)


def _submission(*, provider: BrowserProvider) -> BrowserTaskSubmission:
    return BrowserTaskSubmission(
        provider=provider,
        kind=BrowserVertical.LODGING,
        query=BrowserSearchQuery(
            destination="Hulhumalé",
            start_date="2026-08-20",
            end_date="2026-08-27",
            adults=2,
            rooms=1,
        ),
        timeout_seconds=120,
    )


def _snapshot(
    *,
    retryable: bool,
    code: str = "timeout",
    preserved: bool,
) -> BrowserTaskSnapshot:
    details = {}
    if preserved:
        details["preserved_exact_result_tab"] = {
            "provider": "qunar",
            "kind": "lodging",
            "tab_id": 25,
            "url": "https://hotel.qunar.com/intl/search.jsp",
        }
    return BrowserTaskSnapshot(
        id="browser-task-test-reuse",
        provider=BrowserProvider.QUNAR,
        kind=BrowserVertical.LODGING,
        query=_submission(provider=BrowserProvider.QUNAR).query,
        state=BrowserTaskState.FAILED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        attempt_count=1,
        failure=BrowserFailure(
            code=code,
            message="browser companion did not complete the task before its lease expired",
            retryable=retryable,
            captured_at=datetime.now(UTC),
            details=details,
        ),
    )


def test_preserved_result_tab_triggers_reuse_flag() -> None:
    submission = _submission(provider=BrowserProvider.QUNAR)
    terminal = _snapshot(retryable=True, preserved=True)
    assert _should_reuse_lodging_result_tab(terminal, submission) is True

    retry = _with_reuse_lodging_result_tab(submission)
    assert retry is not submission
    assert retry.query.options["__tripchord_reuse_exact_result_tab"] is True
    # The original submission is not mutated.
    assert "__tripchord_reuse_exact_result_tab" not in submission.query.options
    # The retry still carries the same search contract.
    assert retry.provider == BrowserProvider.QUNAR
    assert retry.kind == BrowserVertical.LODGING
    assert retry.query.start_date == submission.query.start_date


def test_no_reuse_without_preserved_tab() -> None:
    submission = _submission(provider=BrowserProvider.QUNAR)
    terminal = _snapshot(retryable=True, preserved=False)
    assert _should_reuse_lodging_result_tab(terminal, submission) is False


def test_no_reuse_for_non_retryable_failure() -> None:
    submission = _submission(provider=BrowserProvider.QUNAR)
    terminal = _snapshot(retryable=False, preserved=True)
    assert _should_reuse_lodging_result_tab(terminal, submission) is False


def test_no_reuse_for_ctrip() -> None:
    submission = _submission(provider=BrowserProvider.CTRIP)
    terminal = _snapshot(retryable=True, preserved=True)
    assert _should_reuse_lodging_result_tab(terminal, submission) is False


def test_no_reuse_when_search_url_is_present() -> None:
    submission = _submission(provider=BrowserProvider.QUNAR)
    submission = submission.model_copy(
        update={
            "query": submission.query.model_copy(
                update={
                    "search_url": (
                        "https://hotel.qunar.com/intl/search.jsp"
                        "?toCity=%E8%83%A1%E9%B2%81%E9%A9%AC%E7%B4%AF"
                        "&fromDate=2026-08-20&toDate=2026-08-27"
                        "&cityurl=i-hulhumale&from=globalhotelpages"
                    )
                }
            )
        }
    )
    terminal = _snapshot(retryable=True, preserved=True)
    assert _should_reuse_lodging_result_tab(terminal, submission) is False


def test_no_reuse_for_flight() -> None:
    submission = _submission(provider=BrowserProvider.QUNAR)
    submission = submission.model_copy(
        update={
            "kind": BrowserVertical.FLIGHT,
            "query": submission.query.model_copy(
                update={"origin": "杭州"},
            ),
        }
    )
    terminal = _snapshot(retryable=True, preserved=True)
    assert _should_reuse_lodging_result_tab(terminal, submission) is False
