from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import start_live_api

FIRST_TOKEN = "a" * 64
SECOND_TOKEN = "b" * 64


def test_bridge_secret_is_created_with_mode_0600_and_persists(tmp_path: Path) -> None:
    token_file = tmp_path / ".runtime" / "browser-bridge-token"

    token, created = start_live_api.load_or_create_bridge_token(
        token_file,
        token_factory=lambda: FIRST_TOKEN,
    )

    assert token == FIRST_TOKEN
    assert created is True
    assert token_file.read_text(encoding="utf-8") == f"{FIRST_TOKEN}\n"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700


def test_bridge_secret_is_reused_without_calling_token_factory(tmp_path: Path) -> None:
    token_file = tmp_path / ".runtime" / "browser-bridge-token"
    start_live_api.load_or_create_bridge_token(
        token_file,
        token_factory=lambda: FIRST_TOKEN,
    )

    def unexpected_factory() -> str:
        raise AssertionError("an existing bridge secret must be reused")

    token, created = start_live_api.load_or_create_bridge_token(
        token_file,
        token_factory=unexpected_factory,
    )

    assert token == FIRST_TOKEN
    assert created is False


def test_existing_bridge_secret_with_unsafe_permissions_is_rejected(tmp_path: Path) -> None:
    runtime_directory = tmp_path / ".runtime"
    runtime_directory.mkdir(mode=0o700)
    runtime_directory.chmod(0o700)
    token_file = runtime_directory / "browser-bridge-token"
    token_file.write_text(f"{FIRST_TOKEN}\n", encoding="utf-8")
    token_file.chmod(0o640)

    with pytest.raises(SystemExit, match="0600"):
        start_live_api.load_or_create_bridge_token(token_file)

    assert stat.S_IMODE(token_file.stat().st_mode) == 0o640


def test_launcher_never_echoes_secret_and_keeps_it_after_api_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / ".runtime" / "browser-bridge-token"
    start_live_api.load_or_create_bridge_token(
        token_file,
        token_factory=lambda: FIRST_TOKEN,
    )
    observed_environment: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        command = args[0]
        assert isinstance(command, list)
        assert FIRST_TOKEN not in " ".join(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        observed_environment.update(environment)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(start_live_api.subprocess, "run", fake_run)

    assert start_live_api.run_live_api(port=8000, repository=tmp_path) == 0

    output = capsys.readouterr()
    assert FIRST_TOKEN not in output.out
    assert FIRST_TOKEN not in output.err
    assert observed_environment["TRIPCHORD_BROWSER_BRIDGE_TOKEN"] == FIRST_TOKEN
    assert observed_environment["TRIPCHORD_BROWSER_BRIDGE_ENABLED"] == "true"
    assert observed_environment["TRIPCHORD_BROWSER_COMPANION_AUTO_RELOAD_ENABLED"] == "true"
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8") == f"{FIRST_TOKEN}\n"


def test_existing_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    runtime_directory = tmp_path / ".runtime"
    runtime_directory.mkdir(mode=0o700)
    runtime_directory.chmod(0o700)
    target = tmp_path / "outside-secret"
    target.write_text(f"{SECOND_TOKEN}\n", encoding="utf-8")
    target.chmod(0o600)
    token_file = runtime_directory / "browser-bridge-token"
    token_file.symlink_to(target)

    with pytest.raises(SystemExit, match="不能是符号链接"):
        start_live_api.load_or_create_bridge_token(token_file)
