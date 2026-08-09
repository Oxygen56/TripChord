from __future__ import annotations

from typing import Any

import httpx
import pytest
from tripchord.runtime_provenance import local_expected_provenance

from scripts import verify_api_runtime_provenance as verify


def _provenance(**overrides: Any) -> dict[str, Any]:
    expected = local_expected_provenance()
    return {
        "repo_toplevel": expected["repo_toplevel"],
        "commit_sha": expected["commit_sha"],
        "dependency_lock_sha256": expected["dependency_lock_sha256"],
        "live_system_source_sha256": expected["live_system_source_sha256"],
        "started_at": "2026-08-10T00:00:00+00:00",
        "pid": 4242,
        "python_version": "3.12",
        "python_executable": "/usr/bin/python3",
        **overrides,
    }


def _runtime(provenance: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_enabled": True,
        "model_required": True,
    }
    if provenance is not None:
        payload["runtime_provenance"] = provenance
    return payload


def test_verify_exits_0_on_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "_fetch_runtime", lambda base, timeout: _runtime(_provenance()))
    assert verify.main(["--quiet"]) == 0


def test_verify_exits_2_on_old_api_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker started before the current HEAD (stale startup commit) exits 2."""
    monkeypatch.setattr(
        verify,
        "_fetch_runtime",
        lambda base, timeout: _runtime(_provenance(commit_sha="1" * 40)),
    )
    assert verify.main(["--quiet"]) == 2


def test_verify_exits_2_on_head_changed_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD moved after the worker started and it was not restarted -> exit 2."""
    monkeypatch.setattr(
        verify,
        "_fetch_runtime",
        lambda base, timeout: _runtime(_provenance(commit_sha="2" * 40)),
    )
    assert verify.main(["--quiet"]) == 2


def test_verify_exits_2_on_wrong_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running live_system source differs from the current tree -> exit 2."""
    monkeypatch.setattr(
        verify,
        "_fetch_runtime",
        lambda base, timeout: _runtime(_provenance(live_system_source_sha256="f" * 64)),
    )
    assert verify.main(["--quiet"]) == 2


def test_verify_exits_2_on_wrong_lock_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verify,
        "_fetch_runtime",
        lambda base, timeout: _runtime(_provenance(dependency_lock_sha256="e" * 64)),
    )
    assert verify.main(["--quiet"]) == 2


def test_verify_exits_2_when_runtime_carries_no_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "_fetch_runtime", lambda base, timeout: _runtime(None))
    assert verify.main(["--quiet"]) == 2


def test_verify_exits_2_when_api_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_base: str, _timeout: float) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(verify, "_fetch_runtime", _raise)
    assert verify.main(["--quiet"]) == 2
