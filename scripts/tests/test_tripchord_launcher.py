"""v0.8 launcher contract: check/wizard run without a live stack."""

from __future__ import annotations

import argparse

from scripts.tripchord_launcher import cmd_check, cmd_wizard


def _args() -> argparse.Namespace:
    return argparse.Namespace()


def test_check_returns_zero_when_prerequisites_met(monkeypatch) -> None:
    import scripts.tripchord_launcher as launcher

    monkeypatch.setattr(launcher, "_check_prerequisite", lambda *_: "/usr/bin/tool")
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_, **__: type(
        "R", (), {"returncode": 0}
    )())
    assert cmd_check(_args()) == 0


def test_wizard_never_touches_network(monkeypatch) -> None:

    # Force the LLM-key and registry branches to their offline paths.
    monkeypatch.delenv("TRIPCHORD_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert cmd_wizard(_args()) == 0


def test_launcher_runtime_dir_is_private(tmp_path) -> None:
    import stat

    runtime = tmp_path / ".runtime"
    runtime.mkdir(mode=0o700)
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700


def test_wizard_reports_unauthorised_model_smoke(monkeypatch, capsys) -> None:

    monkeypatch.delenv("TRIPCHORD_ACK_MODEL_COST", raising=False)
    assert cmd_wizard(_args()) == 0
    captured = capsys.readouterr()
    assert "未授权（不会发起付费模型调用）" in captured.out
