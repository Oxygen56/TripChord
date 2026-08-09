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


def _populate_required_evidence(staging_dir: Path) -> None:
    """Write the fixed required raw-evidence inputs into staging so main()'s
    evidence-contract gate passes and the commit phase is actually exercised."""
    staging_dir.mkdir(exist_ok=True)
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "browser-e2e.json").write_text('{"passed": true}\n', encoding="utf-8")
    (staging_dir / "browser-e2e-screenshot.png").write_bytes(b"PNG")
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(
            {
                "passed": True,
                "bridge_token_present": True,
                "scopes": [{"scope": "x", "passed": True}],
                "companion_status": {"companions": []},
            }
        ),
        encoding="utf-8",
    )
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"run_status": "completed"}\n', encoding="utf-8"
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
# layer 6 API runtime provenance cross-check (round 14 counter-examples)
# ---------------------------------------------------------------------------


def _matching_runner_evidence(root: Path) -> dict[str, object]:
    # Temp repos have no uv.lock / live_system.py, so the expected fingerprints
    # are None; the matching runtime also reports None, leaving toplevel and
    # commit_sha as the meaningful compared fields.
    return {
        "run_status": "completed",
        "passed": True,
        "repo_revision": {
            "toplevel": str(root),
            "branch": "main",
            "commit_sha": _head(root),
            "worktree_dirty": False,
        },
        "runtime_before_run": {
            "runtime_provenance": {
                "repo_toplevel": str(root),
                "commit_sha": _head(root),
                "dependency_lock_sha256": None,
                "live_system_source_sha256": None,
                "started_at": "2026-08-10T00:00:00+00:00",
                "pid": os.getpid(),
                "python_version": "3.12",
                "python_executable": "/usr/bin/python3",
            }
        },
    }


def test_runner_runtime_provenance_accepts_matching(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    mismatches = gate._runtime_provenance_mismatches(
        _matching_runner_evidence(clean_repo), clean_repo
    )
    assert mismatches == []


def test_runner_runtime_provenance_mismatches_stale_commit(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"]["commit_sha"] = "0" * 40  # type: ignore[index]
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("commit_sha" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_wrong_live_system_fingerprint(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"][  # type: ignore[index]
        "live_system_source_sha256"
    ] = "f" * 64
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("live_system_source_sha256" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_wrong_lock_fingerprint(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"][  # type: ignore[index]
        "dependency_lock_sha256"
    ] = "e" * 64
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("dependency_lock_sha256" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_foreign_toplevel(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"]["repo_toplevel"] = "/elsewhere"  # type: ignore[index]
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("repo_toplevel" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_missing_bundle(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    mismatches = gate._runtime_provenance_mismatches({}, clean_repo)
    assert any("runtime_before_run" in item for item in mismatches)


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


# ---------------------------------------------------------------------------
# round 14 follow-up: four fail-closed hard gates, real temp-repo lifecycle
# ---------------------------------------------------------------------------
# The supervisor's independent review found four fail-closed gaps that the
# monkeypatch-heavy suite could not prove.  These counter-examples exercise the
# real git lifecycle (real temp repo, real commits, real hook failures, real
# file parsing) and assert exit 2 end-to-end through ``main()``.  Only the
# layer *execution* is stubbed — the layer body would otherwise require the
# full product stack — the git/evidence/commit machinery is real.


def test_git_check_raises_on_nonzero_exit(tmp_path: Path) -> None:
    """Defect fix: ``_git(..., check=True)`` must fail closed on a non-zero
    git exit instead of returning an unreadable stdout that callers consume as
    'unknown'."""
    with pytest.raises(gate.GateStateChangedError, match="exit 128"):
        gate._git("rev-parse", "HEAD", cwd=tmp_path, check=True)


def test_main_exits_2_when_evidence_commit_fails(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Real git lifecycle counter-example: a failed ``git commit`` (a real
    pre-commit hook that exits non-zero) during the evidence-commit phase must
    abort the gate with exit 2 — a commit failure must never be swallowed and
    reported as a pass."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    hook = clean_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    rc = gate.main(
        ["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"]
    )

    assert rc == 2


def test_main_exits_2_when_head_moves_before_evidence_commit(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Real git lifecycle counter-example: if a concurrent writer moves HEAD
    between the run and the evidence-commit phase, the commit-phase snapshot
    must disagree with the tested commit and the gate must exit 2 (TOCTOU)."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)

    real_dump = gate._dump
    moved = False

    def dump_then_move(report: gate.GateReport, output_path: Path) -> Path:
        nonlocal moved
        if not moved:
            moved = True
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clean_repo),
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "concurrent writer moved HEAD",
                ],
                check=True,
            )
        return real_dump(report, output_path)

    monkeypatch.setattr(gate, "_dump", dump_then_move)

    rc = gate.main(
        ["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"]
    )

    assert rc == 2


def test_main_exits_2_when_output_is_tracked_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Real git lifecycle counter-example: a tracked repository path must be
    rejected as ``--output`` (exit 2) — the gate must never write evidence
    into a path that could rewrite tracked files after the clean check."""
    _patch_root(monkeypatch, clean_repo)
    tracked_output = clean_repo / "tracked.txt"

    rc = gate.main(
        [
            "--staging-dir",
            str(staging_dir),
            "--output",
            str(tracked_output),
            "--quiet",
        ]
    )

    assert rc == 2


def test_main_exits_2_when_output_is_unignored_in_repo_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """An untracked-but-not-ignored in-repo path must also be rejected as
    ``--output`` (exit 2)."""
    _patch_root(monkeypatch, clean_repo)
    unignored_output = clean_repo / "unignored-dir" / "gate.json"
    unignored_output.parent.mkdir()

    rc = gate.main(
        [
            "--staging-dir",
            str(staging_dir),
            "--output",
            str(unignored_output),
            "--quiet",
        ]
    )

    assert rc == 2


def test_main_exits_2_when_runner_evidence_missing_fields(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Real git lifecycle counter-example: a layer-6 evidence bundle whose
    ``repo_revision`` omits ``toplevel`` and ``run_status`` must fail closed —
    the gate must not accept a runner that cannot name the repository or
    confirm completion."""
    _patch_root(monkeypatch, clean_repo)
    for name in (
        "layer1_reproducibility",
        "layer2_replay",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda *args, name=name: gate.LayerResult(name=name, passed=True),
        )
    staging_dir.mkdir()
    evidence_path = staging_dir / "live-done-gate-v4.json"
    evidence: dict[str, object] = {
        "run_status": None,
        "passed": True,
        "repo_revision": {
            "toplevel": None,
            "branch": "main",
            "commit_sha": _head(clean_repo),
            "worktree_dirty": False,
        },
        "runtime_before_run": {
            "runtime_provenance": {
                "repo_toplevel": str(clean_repo),
                "commit_sha": _head(clean_repo),
                "dependency_lock_sha256": None,
                "live_system_source_sha256": None,
                "started_at": "2026-08-10T00:00:00+00:00",
                "pid": os.getpid(),
                "python_version": "3.12",
                "python_executable": "/usr/bin/python3",
            }
        },
    }

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(gate, "_run", fake_run)
    monkeypatch.setenv("TRIPCHORD_BROWSER_BRIDGE_TOKEN", "t" * 40)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")

    rc = gate.main(["--staging-dir", str(staging_dir), "--quiet"])

    assert rc == 2


def test_commit_evidence_refuses_toplevel_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Direct guard test: the commit-phase snapshot must name the same
    repository the report records."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        toplevel="/elsewhere",
        worktree_dirty=False,
    )
    start = _expected_snapshot(clean_repo)
    with pytest.raises(gate.GateStateChangedError, match="toplevel"):
        gate._commit_evidence(staging_dir, report, start=start)


def test_commit_evidence_refuses_tested_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Direct guard test: the commit-phase HEAD must equal the tested commit."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha="d" * 40,
        toplevel=str(clean_repo),
        worktree_dirty=False,
    )
    start = _expected_snapshot(clean_repo)
    with pytest.raises(gate.GateStateChangedError, match="tested commit"):
        gate._commit_evidence(staging_dir, report, start=start)


def test_runner_revision_mismatches_missing_toplevel(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """Defect fix: a runner that could not determine its repository must fail
    closed instead of being accepted with ``toplevel=None``."""
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["repo_revision"]["toplevel"] = None  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("toplevel" in item for item in mismatches)


def test_runner_revision_mismatches_missing_run_status(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """Defect fix: a runner that did not report completion must fail closed
    instead of being accepted with ``run_status=None``."""
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_evidence(clean_repo)
    evidence["run_status"] = None  # type: ignore[index]
    mismatches = gate._runner_revision_mismatches(
        evidence, _expected_snapshot(clean_repo), clean_repo
    )
    assert any("run_status" in item for item in mismatches)


# ---------------------------------------------------------------------------
# round 15 follow-up (supervisor 04:00 final review): staging-dir pre-write
# validation ordering, commit-phase parent binding, fail-closed on-disk report,
# and runtime provenance Python/start-time/process hard checks.  These are real
# temp-repo lifecycle counter-examples: only layer execution is stubbed, the
# git/validation/commit machinery is real.
# ---------------------------------------------------------------------------


def _porcelain(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_main_exits_2_when_staging_is_tracked_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """A tracked repository path must be rejected as ``--staging-dir`` with
    exit 2, leaving the porcelain byte-for-byte unchanged."""
    _patch_root(monkeypatch, clean_repo)
    before = _porcelain(clean_repo)
    rc = gate.main(["--staging-dir", str(clean_repo / "tracked.txt"), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before


def test_main_exits_2_when_staging_is_unignored_in_repo_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """An un-ignored in-repo ``--staging-dir`` must be rejected *before* any
    mkdir: the untracked directory must not exist afterwards (no side-effect
    creation before validation)."""
    _patch_root(monkeypatch, clean_repo)
    before = _porcelain(clean_repo)
    unignored = clean_repo / "unignored-dir" / "evidence"
    rc = gate.main(["--staging-dir", str(unignored), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before
    assert not unignored.exists()  # the directory was never created


def test_main_exits_2_when_staging_is_file_conflict(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """A ``--staging-dir`` that already exists as a file is a file conflict:
    the gate must exit 2 instead of surfacing a raw FileExistsError."""
    _patch_root(monkeypatch, clean_repo)
    before = _porcelain(clean_repo)
    conflict = tmp_path / "staging-file"
    conflict.write_text("not a directory\n", encoding="utf-8")
    rc = gate.main(["--staging-dir", str(conflict), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before


def test_main_exits_2_when_output_is_directory_conflict(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """A ``--output`` that already exists as a directory is rejected with exit
    2 instead of a raw IsADirectoryError from os.replace."""
    _patch_root(monkeypatch, clean_repo)
    before = _porcelain(clean_repo)
    conflict = tmp_path / "output-as-dir"
    conflict.mkdir()
    rc = gate.main(
        ["--staging-dir", str(staging_dir), "--output", str(conflict), "--quiet"]
    )
    assert rc == 2
    assert _porcelain(clean_repo) == before


def test_commit_evidence_fails_when_head_moves_after_entry_snapshot(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example: HEAD moves *after* the entry snapshot but before the
    commit — the re-verify must abort before writing anything, leaving the
    tree untouched."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=[gate.LayerResult(name="6_full_e2e", passed=True)],
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)
    # Concurrent writer moves HEAD after the entry snapshot.
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "--allow-empty", "-q", "-m", "moved"],
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="HEAD moved"):
        gate._commit_evidence(staging_dir, report, start=start)
    # No tracked report was written and the porcelain is unchanged.
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    assert not report_path.exists()
    assert _porcelain(clean_repo) == ""


def test_commit_evidence_fails_on_real_git_add_error(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example: a real ``git add`` failure (an existing index.lock)
    must abort the phase, and the working tree must be rolled back so no
    passed=true report without an evidence commit is left on disk."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=[gate.LayerResult(name="6_full_e2e", passed=True)],
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)
    (clean_repo / ".git" / "index.lock").write_text("locked\n", encoding="utf-8")

    with pytest.raises(gate.GateStateChangedError, match="failed"):
        gate._commit_evidence(staging_dir, report, start=start)

    # Fail-closed on disk: no tracked report/evidence was left behind.
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    assert not report_path.exists()
    assert _porcelain(clean_repo) == ""


def test_main_commit_evidence_failure_marks_report_failed_on_disk(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A failed --commit-evidence phase must hard-fail the process AND write an
    on-disk report whose JSON carries passed=false — asserting fields, not
    just the return code."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    hook = clean_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])
    assert rc == 2

    report_path = staging_dir / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["evidence_commit"] is None
    assert "evidence commit failed" in payload["summary"]
    # The repository was rolled back to a clean tree after the failure.
    assert _porcelain(clean_repo) == ""


def test_commit_evidence_skips_gitignored_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example: the repository ignores ``benchmarks/results/live-*``,
    so the evidence commit must skip those targets (a ``git add`` on an ignored
    path fails closed and aborts the phase) while still committing the
    committable evidence — E never claims to carry the ignored files."""
    _patch_root(monkeypatch, clean_repo)
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )
    staging_dir.mkdir()
    for name in (
        "product-acceptance.json",
        "browser-e2e.json",
        "browser-e2e-screenshot.png",
        "live-canary-certified.json",
        "live-done-gate-v4.json",
    ):
        (staging_dir / name).write_bytes(b"{}\n" if name.endswith(".json") else b"PNG")

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

    # The ignored live-* evidence is NOT part of E's tree.
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "diff",
            "--name-only",
            tested_sha,
            evidence_commit,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert diff != ""
    for path in diff.splitlines():
        assert "live-" not in path, f"ignored evidence committed: {path}"
    # Committable evidence and the report landed; the tree is clean.
    assert (clean_repo / "benchmarks" / "results" / "product-acceptance.json").is_file()
    assert (clean_repo / "benchmarks" / "results" / "browser-e2e-screenshot.png").is_file()
    assert _porcelain(clean_repo) == ""
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["evidence_commit"] == evidence_commit


def test_restore_tracked_file_handles_binary_blob(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """Counter-example: ``_restore_tracked_file`` must restore a binary blob
    (PNG) byte-for-byte without crashing on UTF-8 decoding of raw bytes."""
    _patch_root(monkeypatch, clean_repo)
    png = clean_repo / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 10)
    subprocess.run(["git", "-C", str(clean_repo), "add", "shot.png"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "add png"], check=True
    )
    original = png.read_bytes()
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff\xfe\xfd" * 10)
    gate._restore_tracked_file("shot.png")
    assert png.read_bytes() == original
    assert _porcelain(clean_repo) == ""


def test_commit_evidence_rollback_handles_binary_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example: a real ``git add`` failure during the evidence commit
    must roll the working tree back cleanly even when a binary PNG is among the
    written evidence — the rollback must not crash on the binary blob."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "browser-e2e-screenshot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 10
    )
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=[gate.LayerResult(name="6_full_e2e", passed=True)],
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)
    (clean_repo / ".git" / "index.lock").write_text("locked\n", encoding="utf-8")

    with pytest.raises(gate.GateStateChangedError, match="failed"):
        gate._commit_evidence(staging_dir, report, start=start)

    # No tracked report/evidence left behind, and no crash on the binary blob.
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    assert not report_path.exists()
    assert _porcelain(clean_repo) == ""


def test_main_commit_evidence_skips_gitignored_evidence_end_to_end(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """End-to-end: a clean run whose staged evidence includes git-ignored
    live-* files must still commit the committable evidence and exit 0 — the
    gate never aborts on ignored evidence it cannot commit."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )
    _populate_required_evidence(staging_dir)

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])
    assert rc == 0
    report_path = staging_dir / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["evidence_commit"] is not None
    assert _porcelain(clean_repo) == ""


def test_main_failed_gate_never_commits_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A1 counter-example: a gate that fails MUST NEVER commit evidence.  With
    --commit-evidence, a failed run keeps the evidence in the ignored/out-of-repo
    staging dir and exits 2, leaving HEAD, commit parentage, porcelain and the
    tracked results tree byte-for-byte unchanged — no _commit_evidence, no new
    commit, no staged or tracked writes."""
    _patch_root(monkeypatch, clean_repo)
    for name in (
        "layer1_reproducibility",
        "layer2_replay",
        "layer3_clean_chrome_fixtures",
        "layer4_model_smoke",
        "layer5_real_canary",
    ):
        monkeypatch.setattr(
            gate,
            name,
            lambda *args, name=name: gate.LayerResult(name=name, passed=True),
        )
    monkeypatch.setattr(
        gate,
        "layer6_full_e2e",
        lambda *args: gate.LayerResult(
            name="6_full_e2e", passed=False, detail="real e2e failure"
        ),
    )
    staging_dir.mkdir()
    for name in (
        "product-acceptance.json",
        "browser-e2e.json",
        "browser-e2e-screenshot.png",
        "live-canary-certified.json",
        "live-done-gate-v4.json",
    ):
        (staging_dir / name).write_bytes(b"{}\n" if name.endswith(".json") else b"PNG")

    head_before = _head(clean_repo)
    log_before = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--format=%H %P", "-1"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    porcelain_before = _porcelain(clean_repo)
    tracked_results = clean_repo / "benchmarks" / "results"
    tracked_before = {
        p.relative_to(clean_repo).as_posix(): p.read_bytes()
        for p in sorted(tracked_results.rglob("*"))
        if p.is_file()
    }

    rc = gate.main(
        ["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"]
    )

    assert rc == 2
    assert _head(clean_repo) == head_before
    log_after = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--format=%H %P", "-1"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log_after == log_before
    assert _porcelain(clean_repo) == porcelain_before
    tracked_after = {
        p.relative_to(clean_repo).as_posix(): p.read_bytes()
        for p in sorted(tracked_results.rglob("*"))
        if p.is_file()
    }
    assert tracked_after == tracked_before
    # The failed verdict lives only in the ignored/out-of-repo staging report.
    payload = json.loads(
        (staging_dir / "product-v1-done-gate.json").read_text(encoding="utf-8")
    )
    assert payload["passed"] is False
    assert payload["evidence_commit"] is None


def test_commit_evidence_manifest_records_ignored_and_committed(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A2 counter-example: the committed evidence manifest must record EVERY
    staging original by SHA256 — including git-ignored live-* files that E
    cannot carry — plus redacted layer-5/6 verdict fields.  E must contain the
    manifest and every committable file; the raw ignored evidence stays in the
    staging dir only."""
    _patch_root(monkeypatch, clean_repo)
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(
            {
                "passed": True,
                "bridge_token_present": True,
                "scopes": [{"scope": "x", "passed": True}],
                "companion_status": {
                    "companions": [
                        {
                            "companion_id": "comp-1",
                            "providers": ["p"],
                            "authorized_scope_keys": ["x"],
                            "is_fresh": True,
                            "age_seconds": 3,
                            "build_identity": {"build_sha256": "abc123"},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
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

    # E contains the manifest + committable evidence, never the ignored live-*.
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", evidence_commit],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert gate._MANIFEST_REL in tree
    assert "benchmarks/results/product-acceptance.json" in tree
    assert not any("live-" in p for p in tree)
    # The manifest records both committed and ignored originals by hash.
    manifest = json.loads(
        (clean_repo / gate._MANIFEST_REL).read_text(encoding="utf-8")
    )
    by_name = {entry["name"]: entry for entry in manifest["files"]}
    assert by_name["product-acceptance.json"]["committed"] is True
    assert by_name["live-canary-certified.json"]["committed"] is False
    assert by_name["live-canary-certified.json"]["sha256"] == gate._sha256_file(
        staging_dir / "live-canary-certified.json"
    )
    assert manifest["schema_version"] == gate._MANIFEST_SCHEMA
    assert manifest["tested_commit_sha"] == tested_sha
    assert manifest["evidence_commit"] == evidence_commit
    # Redacted layer-5 verdict: identity + scope verdict, never raw bytes.
    verdict = manifest["layer_verdicts"]["5_real_canary"]
    assert verdict["companion"]["companion_id"] == "comp-1"
    assert verdict["passed"] is True
    assert _porcelain(clean_repo) == ""


def test_verify_evidence_contract_fails_closed_on_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A2 counter-example: the post-commit contract verify must hard-fail (exit 2
    semantics) when E lacks the contract-required manifest."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
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
    manifest = gate._evidence_manifest(staging_dir, report)
    # Commit E WITHOUT the manifest (as if the manifest write was skipped).
    target = clean_repo / "benchmarks" / "results" / "product-acceptance.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"passed": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(clean_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "bare evidence"],
        check=True,
    )
    e_commit = _head(clean_repo)

    with pytest.raises(gate.GateStateChangedError, match="missing required manifest"):
        gate._verify_evidence_contract(e_commit, staging_dir, manifest)


def test_verify_evidence_contract_fails_closed_on_missing_committed_file(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A2 counter-example: the contract verify must hard-fail when E carries the
    manifest but is missing a file the manifest marks committed."""
    _patch_root(monkeypatch, clean_repo)
    staging_dir.mkdir()
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
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
    manifest = gate._evidence_manifest(staging_dir, report)
    # Commit E with the manifest but WITHOUT the committed evidence file it names.
    results = clean_repo / "benchmarks" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / gate._MANIFEST_REL.rsplit("/", 1)[-1]).write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "manifest but no evidence"],
        check=True,
    )
    e_commit = _head(clean_repo)

    with pytest.raises(gate.GateStateChangedError, match="missing committed file"):
        gate._verify_evidence_contract(e_commit, staging_dir, manifest)


def test_layer5_bridge_token_env_only_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1 counter-example: the canary subprocess must receive the bridge token
    via the inherited environment (TRIPCHORD_BROWSER_BRIDGE_TOKEN) and NEVER via
    argv — argv is visible in the process list and logs."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    token = "B" * 64
    monkeypatch.setattr(gate, "_bridge_token", lambda: token)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        calls.append((cmd, kwargs))
        return 0, ""

    monkeypatch.setattr(gate, "_run", fake_run)
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps({"scopes": [], "companion_status": {}}), encoding="utf-8"
    )
    result = gate.layer5_real_canary(staging_dir)
    assert result.passed is True
    assert any(
        any("live_canary_certified.py" in part for part in cmd) for cmd, _ in calls
    )
    for cmd, kwargs in calls:
        if not any("live_canary_certified.py" in part for part in cmd):
            continue
        assert "--bridge-token" not in cmd
        assert token not in cmd
        assert kwargs["env"]["TRIPCHORD_BROWSER_BRIDGE_TOKEN"] == token  # type: ignore[index]


def test_layer6_bridge_token_env_only_not_argv(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """B1 counter-example: the E2E runner subprocess must also get the token via
    env only — never argv."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    token = "C" * 64
    monkeypatch.setattr(gate, "_bridge_token", lambda: token)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        calls.append((cmd, kwargs))
        return 0, ""

    monkeypatch.setattr(gate, "_run", fake_run)
    monkeypatch.setattr(gate, "_runner_revision_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_runtime_provenance_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_extract_build_fingerprint", lambda *a, **k: None)
    start = _expected_snapshot(clean_repo)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"run_status": "completed", "done_gate": {"passed": "True"}}),
        encoding="utf-8",
    )
    result = gate.layer6_full_e2e(staging_dir, start)
    assert result.passed is True
    assert any(
        any("run_live_done_gate_v4.py" in part for part in cmd) for cmd, _ in calls
    )
    for cmd, kwargs in calls:
        if not any("run_live_done_gate_v4.py" in part for part in cmd):
            continue
        assert "--bridge-token" not in cmd
        assert token not in cmd
        assert kwargs["env"]["TRIPCHORD_BROWSER_BRIDGE_TOKEN"] == token  # type: ignore[index]


def test_secret_scan_fails_closed_on_token_in_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """B1 counter-example: a leak of the bridge token into any staging evidence
    file aborts the gate with exit-2 semantics — the token must never reach
    logs or evidence."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    staging_dir.mkdir()
    token = "D" * 64
    monkeypatch.setattr(gate, "_bridge_token", lambda: token)
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"leak": token}), encoding="utf-8"
    )
    with pytest.raises(gate.GateStateChangedError, match="secret leak"):
        gate.run_gate(staging_dir)


def test_runner_runtime_provenance_mismatches_bad_python_version(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"][  # type: ignore[index]
        "python_version"
    ] = "not-a-version"
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("python_version" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_missing_python_version(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"].pop(  # type: ignore[index]
        "python_version"
    )
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("python_version" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_relative_python_executable(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"][  # type: ignore[index]
        "python_executable"
    ] = ".venv/bin/python"
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("python_executable" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_bad_started_at(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"]["started_at"] = "yesterday-ish"  # type: ignore[index]
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("started_at" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_missing_started_at(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"].pop("started_at")  # type: ignore[index]
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("started_at" in item for item in mismatches)


def test_runner_runtime_provenance_mismatches_dead_pid(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    _patch_root(monkeypatch, clean_repo)
    evidence = _matching_runner_evidence(clean_repo)
    evidence["runtime_before_run"]["runtime_provenance"]["pid"] = 2147483647  # type: ignore[index]
    mismatches = gate._runtime_provenance_mismatches(evidence, clean_repo)
    assert any("pid" in item for item in mismatches)


# ---------------------------------------------------------------------------
# Evidence-contract required inputs (A2: layer-5/6 raw evidence gate)
# ---------------------------------------------------------------------------


def test_main_exits_2_when_required_evidence_missing(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Evidence-contract counter-example: a clean passing run whose staging dir
    lacks a fixed required raw evidence input (layer-6 raw evidence) must exit
    2 at the contract gate and must NOT produce any commit — the missing file
    is never silently omitted from the committed trail."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    # Delete the layer-6 raw evidence — the contract-required input.
    (staging_dir / "live-done-gate-v4.json").unlink()

    head_before = _head(clean_repo)
    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 2
    assert _head(clean_repo) == head_before
    assert _porcelain(clean_repo) == ""


def test_main_exits_2_when_layer5_raw_evidence_missing(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Evidence-contract counter-example: missing layer-5 raw evidence
    (live-canary-certified.json) also hard-fails the contract gate exit 2."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    (staging_dir / "live-canary-certified.json").unlink()

    head_before = _head(clean_repo)
    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 2
    assert _head(clean_repo) == head_before


def test_verify_required_evidence_inputs_accepts_full_set(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """The required-input verifier accepts a complete staging set and fails
    closed on any missing member."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    gate._verify_required_evidence_inputs(staging_dir)  # no raise

    (staging_dir / "browser-e2e.json").unlink()
    with pytest.raises(gate.GateStateChangedError, match="browser-e2e.json"):
        gate._verify_required_evidence_inputs(staging_dir)


def test_verify_evidence_contract_fails_closed_on_incomplete_manifest_fields(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A2 counter-example: the post-commit contract verify must hard-fail when
    E's manifest omits a required contract field (field-completeness of the
    committed trail), even when every committed file hash matches."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
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
    manifest = gate._evidence_manifest(staging_dir, report)
    manifest.pop("layer_verdicts")
    results = clean_repo / "benchmarks" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / gate._MANIFEST_REL.rsplit("/", 1)[-1]).write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    for name in ("product-acceptance.json", "browser-e2e.json", "browser-e2e-screenshot.png"):
        (results / name).write_bytes((staging_dir / name).read_bytes())
    subprocess.run(["git", "-C", str(clean_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "manifest missing fields"],
        check=True,
    )
    e_commit = _head(clean_repo)

    with pytest.raises(gate.GateStateChangedError, match="layer_verdicts"):
        gate._verify_evidence_contract(e_commit, staging_dir, manifest)
