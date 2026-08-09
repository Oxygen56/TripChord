from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import run_product_done_gate as gate


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "gate-test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Gate Test"],
        check=True,
    )
    tracked = root / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "baseline"],
        check=True,
    )


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    return tmp_path


def test_worktree_dirty_false_on_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
) -> None:
    monkeypatch.setattr(gate, "ROOT", clean_repo)
    assert gate._worktree_dirty() is False


def test_worktree_dirty_true_with_uncommitted_change(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
) -> None:
    (clean_repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", clean_repo)
    assert gate._worktree_dirty() is True


def test_worktree_dirty_true_with_untracked_file(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
) -> None:
    (clean_repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", clean_repo)
    assert gate._worktree_dirty() is True


def test_run_gate_forces_failed_on_dirty_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "layer1_reproducibility",
        "layer2_replay",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
        "layer6_full_e2e",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda name=name: gate.LayerResult(name=name, passed=True),
        )
    monkeypatch.setattr(gate, "_worktree_dirty", lambda: True)

    report = gate.run_gate(commit="deadbeef")

    assert report.worktree_dirty is True
    assert report.passed is False
    assert "uncommitted" in report.summary
    assert report.commit_sha == "deadbeef"


def test_run_gate_clean_tree_can_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "layer1_reproducibility",
        "layer2_replay",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
        "layer6_full_e2e",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda name=name: gate.LayerResult(name=name, passed=True),
        )
    monkeypatch.setattr(gate, "_worktree_dirty", lambda: False)

    report = gate.run_gate(commit="cafe1234")

    assert report.worktree_dirty is False
    assert report.passed is True
    assert report.commit_sha == "cafe1234"
