from __future__ import annotations

import json
import os
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
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    return repo


@pytest.fixture()
def staging_dir(tmp_path: Path) -> Path:
    # A sibling of the repo root, so an in-repo untracked dir never pollutes
    # the git status the gate snapshots.
    return tmp_path / "staging"


def _head(root: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _patch_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "RESULTS_DIR", root / "benchmarks" / "results")


def _passing_layers(monkeypatch: pytest.MonkeyPatch) -> None:
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
            lambda *args, name=name: gate.LayerResult(name=name, passed=True),
        )


# ---------------------------------------------------------------------------
# _git_safe_env / _git_snapshot / _verify_tree_unchanged
# ---------------------------------------------------------------------------


def test_git_safe_env_drops_git_dir_override() -> None:
    os.environ["GIT_DIR"] = "/evil/repo"
    os.environ["GIT_WORK_TREE"] = "/evil/worktree"
    os.environ["GIT_INDEX_FILE"] = "/evil/index"
    os.environ["GIT_CONFIG_GLOBAL"] = "/real/config"
    try:
        env = gate._git_safe_env()
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env
        assert "GIT_INDEX_FILE" not in env
        # Global config is explicitly allowed through.
        assert env.get("GIT_CONFIG_GLOBAL") == "/real/config"
    finally:
        os.environ.pop("GIT_DIR", None)
        os.environ.pop("GIT_WORK_TREE", None)
        os.environ.pop("GIT_INDEX_FILE", None)
        os.environ.pop("GIT_CONFIG_GLOBAL", None)


def test_git_snapshot_identifies_committed_revision(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    snapshot = gate._git_snapshot()
    assert snapshot.toplevel == str(clean_repo)
    assert snapshot.commit_sha == _head(clean_repo)
    assert snapshot.worktree_dirty is False
    assert snapshot.branch is not None


def test_git_snapshot_sees_uncommitted_change(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    (clean_repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    snapshot = gate._git_snapshot()
    assert snapshot.worktree_dirty is True
    assert "tracked.txt" in snapshot.porcelain


def test_verify_tree_unchanged_raises_on_head_move() -> None:
    start = gate.GitSnapshot(
        toplevel="/repo",
        branch="main",
        commit_sha="aaaa",
        worktree_dirty=False,
        porcelain="",
    )
    end = gate.GitSnapshot(
        toplevel="/repo",
        branch="main",
        commit_sha="bbbb",
        worktree_dirty=False,
        porcelain="",
    )
    with pytest.raises(gate.GateStateChangedError, match="HEAD"):
        gate._verify_tree_unchanged(start, end)


def test_verify_tree_unchanged_raises_on_porcelain_change() -> None:
    start = gate.GitSnapshot(
        toplevel="/repo", branch="main", commit_sha="aaaa", worktree_dirty=False, porcelain=""
    )
    end = gate.GitSnapshot(
        toplevel="/repo",
        branch="main",
        commit_sha="aaaa",
        worktree_dirty=True,
        porcelain=" M tracked.txt\n",
    )
    with pytest.raises(gate.GateStateChangedError, match="porcelain"):
        gate._verify_tree_unchanged(start, end)


def test_verify_tree_unchanged_accepts_identical() -> None:
    start = gate.GitSnapshot(
        toplevel="/repo", branch="main", commit_sha="aaaa", worktree_dirty=False, porcelain=""
    )
    gate._verify_tree_unchanged(start, start)


# ---------------------------------------------------------------------------
# run_gate
# ---------------------------------------------------------------------------


def test_run_gate_forces_failed_on_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    (clean_repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    _passing_layers(monkeypatch)

    report = gate.run_gate(staging_dir)

    assert report.worktree_dirty is True
    assert report.passed is False
    assert "uncommitted" in report.summary
    assert report.tested_commit_sha == _head(clean_repo)


def test_run_gate_clean_tree_can_pass(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)

    report = gate.run_gate(staging_dir)

    assert report.worktree_dirty is False
    assert report.passed is True
    assert report.tested_commit_sha == _head(clean_repo)
    assert report.toplevel == str(clean_repo)
    # No evidence commit unless the explicit --commit-evidence phase runs.
    assert report.evidence_commit is None


def test_run_gate_fails_closed_on_requested_commit_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    with pytest.raises(gate.GateStateChangedError, match="cannot certify"):
        gate.run_gate(staging_dir, commit="deadbeef")


def test_run_gate_aborts_when_layer_dirties_tracked_tree(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example (defect fix ②): a layer that writes a tracked file must
    make the gate abort with exit-2 semantics instead of silently certifying a
    tree the gate itself dirtied."""
    _patch_root(monkeypatch, clean_repo)
    for name in (
        "layer1_reproducibility",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
        "layer6_full_e2e",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda *args, name=name: gate.LayerResult(name=name, passed=True),
        )

    def dirtying_layer(staging: Path) -> gate.LayerResult:
        (clean_repo / "tracked.txt").write_text("dirtied by gate\n", encoding="utf-8")
        return gate.LayerResult(name="2_replay", passed=True)

    monkeypatch.setattr(gate, "layer2_replay", dirtying_layer)

    with pytest.raises(gate.GateStateChangedError, match="self-pollution"):
        gate.run_gate(staging_dir)


def test_run_gate_aborts_when_head_moves_mid_run(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example (defect fix ④): if HEAD changes while the gate runs, the
    end snapshot must disagree with the start snapshot and the gate aborts."""
    _patch_root(monkeypatch, clean_repo)
    for name in (
        "layer2_replay",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
        "layer6_full_e2e",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda *args, name=name: gate.LayerResult(name=name, passed=True),
        )

    def moving_layer() -> gate.LayerResult:
        subprocess.run(
            ["git", "-C", str(clean_repo), "commit", "--allow-empty", "-q", "-m", "mid-run move"],
            check=True,
        )
        return gate.LayerResult(name="1_reproducibility", passed=True)

    monkeypatch.setattr(gate, "layer1_reproducibility", moving_layer)

    with pytest.raises(gate.GateStateChangedError, match="HEAD"):
        gate.run_gate(staging_dir)


# ---------------------------------------------------------------------------
# layer 6 revision cross-check (defect fix ③)
# ---------------------------------------------------------------------------


def _expected_snapshot(root: Path) -> gate.GitSnapshot:
    return gate.GitSnapshot(
        toplevel=str(root),
        branch="main",
        commit_sha=_head(root),
        worktree_dirty=False,
        porcelain="",
    )


def _matching_evidence(root: Path) -> dict[str, object]:
    return {
        "run_status": "completed",
        "passed": True,
        "repo_revision": {
            "toplevel": str(root),
            "branch": "main",
            "commit_sha": _head(root),
            "worktree_dirty": False,
        },
    }


def test_runner_revision_mismatches_dirty(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["repo_revision"]["worktree_dirty"] = True  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("worktree_dirty" in item for item in mismatches)


def test_runner_revision_mismatches_wrong_sha(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["repo_revision"]["commit_sha"] = "deadbeef"  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("commit_sha" in item for item in mismatches)


def test_runner_revision_mismatches_foreign_toplevel(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["repo_revision"]["toplevel"] = "/elsewhere"  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("toplevel" in item for item in mismatches)


def test_runner_revision_mismatches_not_completed(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["run_status"] = "done_gate_failed"  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("run_status" in item for item in mismatches)


def test_runner_revision_accepts_matching_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    mismatches = gate._runner_revision_mismatches(
        _matching_evidence(clean_repo), _expected_snapshot(clean_repo), clean_repo
    )
    assert mismatches == []


def test_extract_build_fingerprint() -> None:
    payload = {
        "companions": [
            {"name": "ctrip", "build_identity": {"build_sha256": "a" * 64}},
            {"name": "qunar", "build_identity": {"build_sha256": "b" * 64}},
        ]
    }
    assert gate._extract_build_fingerprint(payload) == "a" * 64
    assert gate._extract_build_fingerprint({"companions": []}) is None
    assert gate._extract_build_fingerprint({}) is None
    assert gate._extract_build_fingerprint(None) is None
    assert gate._extract_build_fingerprint(
        {"companions": [{"build_identity": {"build_sha256": "short"}}]}
    ) is None


# ---------------------------------------------------------------------------
# two-phase evidence commit (defect fix ②)
# ---------------------------------------------------------------------------


def test_commit_evidence_two_phase(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """The evidence commit E must contain only evidence paths, the report must
    record tested_commit_sha=S and evidence_commit=E, and neither may claim E
    was tested at S."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text('{"passed": true}\n', encoding="utf-8")
    (staging_dir / "browser-e2e.json").write_text('{"passed": true}\n', encoding="utf-8")
    (staging_dir / "browser-e2e-screenshot.png").write_bytes(b"PNG")
    (staging_dir / "live-canary-certified.json").write_text(
        '{"scopes": []}\n', encoding="utf-8"
    )
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"run_status": "completed"}\n', encoding="utf-8"
    )

    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=[gate.LayerResult(name="6_full_e2e", passed=True)],
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # Phase 1: E exists as a child of the tested commit, then phase 2 pointer.
    log = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--oneline", "-3"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = [line.strip() for line in log.strip().splitlines()]
    assert len(lines) == 3  # baseline -> E -> pointer

    # The authoritative report (in the tracked results tree) records both SHAs
    # and keeps them distinct.
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["tested_commit_sha"] == tested_sha
    assert payload["evidence_commit"] == evidence_commit
    assert payload["evidence_commit"] != tested_sha
    assert payload["passed"] is True

    # The evidence files landed in E's tree (tracked now).
    assert (clean_repo / "benchmarks" / "results" / "product-acceptance.json").is_file()
    assert (clean_repo / "benchmarks" / "results" / "browser-e2e-screenshot.png").is_file()

    # Working tree clean after the two-phase commit.
    status = subprocess.run(
        ["git", "-C", str(clean_repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""

    # Only evidence paths changed between tested_sha and evidence_commit.
    diff = subprocess.run(
        ["git", "-C", str(clean_repo), "diff", "--name-only", tested_sha, evidence_commit],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert diff != ""
    for path in diff.splitlines():
        assert path.startswith("benchmarks/results/"), f"non-evidence path in E: {path}"
    assert "tracked.txt" not in diff


def test_commit_evidence_refuses_dirty_start(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text('{"passed": true}\n', encoding="utf-8")
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        worktree_dirty=False,
    )
    dirty_start = _expected_snapshot(clean_repo)
    dirty_start = gate.GitSnapshot(
        toplevel=dirty_start.toplevel,
        branch=dirty_start.branch,
        commit_sha=dirty_start.commit_sha,
        worktree_dirty=True,
        porcelain=" M tracked.txt\n",
    )
    with pytest.raises(gate.GateStateChangedError, match="dirty worktree"):
        gate._commit_evidence(staging_dir, report, start=dirty_start)
