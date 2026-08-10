"""Tests for the C-122 round-18 HG-I canary failure-auditability hardening.

A certified canary that crashes must never silently disappear: ``main`` seals a
0600 atomic diagnostic (stage, exception class, desensitized summary, run
identity) next to the evidence and returns a non-zero exit, and the iCom
public-API scope replays a bounded number of times before recording failure.
These tests exercise the failure paths WITHOUT network access.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import live_canary_certified as canary  # noqa: E402


def test_desensitize_redacts_token_shaped_substrings() -> None:
    """A diagnostic summary must never echo a bridge token / bearer secret."""
    message = "search failed with token abcDEF0123456789_abcdefghijklmnopqrstuvwxyz and url ok"
    redacted = canary._desensitize(message)
    assert "abcDEF0123456789" not in redacted
    assert "<redacted>" in redacted
    assert "search failed with" in redacted
    assert "url ok" in redacted


def _fake_icom_provider(
    *, failures_before_success: int = 0, failing_exc: Exception | None = None
) -> mock.AsyncMock:
    """An ``IComTransferProvider`` stub whose ``search`` raises the given failure
    ``failures_before_success`` times, then returns one read-only option."""
    provider = mock.AsyncMock()
    provider.aclose = mock.AsyncMock()

    async def search(_query: Any) -> Any:
        if provider.attempts < failures_before_success:
            provider.attempts += 1
            raise failing_exc or RuntimeError("boom")
        option = mock.Mock()
        option.service_name = "Public Speedboat"
        option.departure_at = mock.Mock()
        option.departure_at.isoformat = mock.Mock(return_value="2026-08-13T10:00:00Z")
        option.fare.amount = "42.00"
        option.fare.currency = "USD"
        result = mock.Mock()
        result.options = [option]
        result.searched_at = mock.Mock()
        result.searched_at.isoformat = mock.Mock(return_value="2026-08-10T00:00:00Z")
        result.source_urls = ["https://example.com/public/read-only"]
        return result

    provider.attempts = 0
    provider.search = mock.AsyncMock(side_effect=search)
    return provider


@pytest.mark.asyncio
async def test_icom_scope_canary_recovers_after_transient_failure() -> None:
    """HG-I recovery replay: a transient provider failure is replayed (bounded),
    and a later success passes with the fresh read-only evidence."""
    provider = _fake_icom_provider(failures_before_success=2)
    with mock.patch.object(canary, "IComTransferProvider", return_value=provider):
        result = await canary._icom_scope_canary()
    assert result["passed"] is True
    assert result["read_only"] is True
    assert result["evidence"]["options"] == 1
    assert provider.attempts == 2


@pytest.mark.asyncio
async def test_icom_scope_canary_failure_records_replay_and_no_secret() -> None:
    """HG-I counter-example: when the replay budget is exhausted the scope is
    recorded FAILED with the attempt count and a desensitized detail — the raw
    exception (which may carry a token) must not propagate."""
    secret = "S" * 64
    provider = _fake_icom_provider(
        failures_before_success=99,
        failing_exc=RuntimeError(f"provider rejected token {secret}"),
    )
    with mock.patch.object(canary, "IComTransferProvider", return_value=provider):
        result = await canary._icom_scope_canary()
    assert result["passed"] is False
    assert result["replay_attempts"] == canary._ICOM_REPLAY_ATTEMPTS
    assert result["exception_class"] == "RuntimeError"
    assert secret not in json.dumps(result)
    assert "<redacted>" in result["detail"]


def test_seal_failure_diagnostic_writes_0600_atomic(tmp_path: Path) -> None:
    """HG-I: the failure diagnostic is sealed next to the evidence with 0600
    perms, carries stage / exception class / desensitized summary / run identity,
    and never echoes the raw exception."""
    output = tmp_path / "live-canary-certified.json"
    secret = "T" * 64
    diag = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(f"wrapped bridge {secret}"),
        output,
    )
    assert diag.exists()
    assert diag.name == "live-canary-certified.json.failure.json"
    assert (diag.stat().st_mode & 0o777) == 0o600
    payload = json.loads(diag.read_text(encoding="utf-8"))
    assert payload["diagnostic_kind"] == "canary_failure"
    assert payload["stage"] == "evaluate"
    assert payload["exception_class"] == "RuntimeError"
    assert secret not in json.dumps(payload)
    assert "<redacted>" in payload["summary"]
    assert payload["run_identity"]["script"] == "live_canary_certified.py"
    # The primary evidence file is NOT produced on a sealed failure.
    assert not output.exists()


def test_main_seals_diagnostic_when_evaluate_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """HG-I counter-example: an exception escaping ``evaluate`` is captured at the
    top level, a 0600 diagnostic is written, the exit code is 1, and the secret is
    not echoed on stderr."""
    output = tmp_path / "live-canary-certified.json"
    secret = "U" * 64

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"unexpected bridge token {secret}")

    monkeypatch.setattr(
        canary.sys,
        "argv",
        [
            "live_canary_certified.py",
            "--api-base",
            "http://127.0.0.1:8000",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(canary, "evaluate", mock.AsyncMock(side_effect=boom))
    code = canary.main()
    assert code == 1
    diag = output.with_suffix(".json.failure.json")
    assert diag.exists()
    assert (diag.stat().st_mode & 0o777) == 0o600
    payload = json.loads(diag.read_text(encoding="utf-8"))
    assert payload["stage"] == "evaluate"
    assert secret not in json.dumps(payload)
    captured = capsys.readouterr()
    assert secret not in captured.err


def test_main_seals_diagnostic_when_dump_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """HG-I: a failure while writing the evidence is also sealed — the canary must
    never exit without a diagnostic on any failure path."""
    output = tmp_path / "live-canary-certified.json"

    monkeypatch.setattr(
        canary.sys,
        "argv",
        [
            "live_canary_certified.py",
            "--api-base",
            "http://127.0.0.1:8000",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(
        canary, "evaluate", mock.AsyncMock(return_value={"passed": True})
    )
    real_dump = canary._dump

    def failing_dump(report: dict[str, Any], out: Path) -> Path:
        if out == output:
            raise OSError("disk full")
        return real_dump(report, out)

    monkeypatch.setattr(canary, "_dump", mock.Mock(side_effect=failing_dump))
    code = canary.main()
    assert code == 1
    diag = output.with_suffix(".json.failure.json")
    assert diag.exists()
    payload = json.loads(diag.read_text(encoding="utf-8"))
    assert payload["stage"] == "dump"
    assert payload["exception_class"] == "OSError"
    captured = capsys.readouterr()
    assert "disk full" in captured.err
