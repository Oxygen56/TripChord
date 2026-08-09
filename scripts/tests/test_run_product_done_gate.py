from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
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
            lambda *args, name=name, **kwargs: gate.LayerResult(
                name=name, passed=True
            ),
        )


def _populating_passing_layers(
    monkeypatch: pytest.MonkeyPatch, staging_dir: Path
) -> None:
    """Mock every layer to pass AND write the raw required evidence into staging,
    modelling the real flow (C-114 R3): main() creates an initially-empty staging
    dir, then the layers populate it — the tests never hand main() a pre-filled
    dir, because the gate refuses reused non-empty staging."""
    monkeypatch.setattr(
        gate,
        "layer1_reproducibility",
        lambda *args, sd=staging_dir: (
            _populate_required_evidence(sd),
            gate.LayerResult(name="layer1_reproducibility", passed=True),
        )[1],
    )
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
            lambda *args, name=name, **kwargs: gate.LayerResult(
                name=name, passed=True
            ),
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
            lambda *args, name=name, **kwargs: gate.LayerResult(
                name=name, passed=True
            ),
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
            lambda *args, name=name, **kwargs: gate.LayerResult(
                name=name, passed=True
            ),
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


def _matching_done_gate() -> dict[str, object]:
    """A passing layer-6 ``done_gate`` report in the real runner schema:
    ``passed`` plus the full 15-item check set, each item passed (the actual
    ``LiveV4DoneGateReport`` shape — there is no top-level ``passed``)."""
    check_names = (
        "prefrozen_stay_plan_candidate_set",
        "v4_source_graph",
        "stage_aware_exploration_publication_contract",
        "stay_inventory_four_state_contract",
        "planner_verifier_repair_master_stay_plan_chain",
        "recommendable_date_pair_stay_plan_options",
        "icom_exploration_and_publication_evidence",
        "all_recommended_publication_closures",
        "real_v4_browser_source_evidence",
        "flight_search_outcome_contract",
        "observed_cross_platform_overlap",
        "strict_selected_plan_platform_coverage",
        "planner_verifier_repair_orchestrator",
        "exact_budget_and_selected_evidence",
        "event_injection_repair_reverify_master",
    )
    return {
        "passed": True,
        "checks": [
            {"name": name, "passed": True, "summary": "ok", "evidence_refs": []}
            for name in check_names
        ],
    }


def _matching_canary() -> dict[str, object]:
    """A passing certified-OTA canary in the real schema: top-level ``passed``
    plus the complete six certified scopes, each fresh/authorized/read_only/
    passed."""
    scopes = (
        ("ctrip:flight", "companion_heartbeat"),
        ("ctrip:lodging", "companion_heartbeat"),
        ("qunar:flight", "companion_heartbeat"),
        ("qunar:lodging", "companion_heartbeat"),
        ("tongcheng:flight", "companion_heartbeat"),
        ("icom:transfer", "icom_public_api"),
    )
    return {
        "passed": True,
        "bridge_token_present": True,
        "scopes": [
            {
                "scope": scope,
                "kind": kind,
                "passed": True,
                "fresh": True,
                "authorized": True,
                "read_only": True,
            }
            for scope, kind in scopes
        ],
        "companion_status": {
            "status": "connected",
            "stale_after_seconds": 45,
            "companions": [
                {
                    "companion_id": "comp-1",
                    "providers": ["ctrip", "qunar", "tongcheng"],
                    "authorized_scope_keys": [scope for scope, _ in scopes],
                    "is_fresh": True,
                    "age_seconds": 3,
                    "build_identity": {"build_sha256": "a" * 64},
                }
            ],
        },
    }


def _matching_evidence(root: Path) -> dict[str, object]:
    return {
        "run_status": "completed",
        "done_gate": _matching_done_gate(),
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
        "done_gate": _matching_done_gate(),
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
# layer 5/6 real-schema contract (C-114 review R1/R2)
# ---------------------------------------------------------------------------


def test_done_gate_mismatches_rejects_forged_top_level_passed() -> None:
    """R1 counter-example: a bundle carrying only a forged top-level ``passed``
    (the schema the real runner never emits) must fail closed — the real
    contract lives under ``done_gate``."""
    mismatches = gate._done_gate_mismatches({"run_status": "completed", "passed": True})
    assert any("no done_gate report" in item for item in mismatches)


def test_done_gate_mismatches_rejects_missing_report() -> None:
    mismatches = gate._done_gate_mismatches({"run_status": "completed"})
    assert any("no done_gate report" in item for item in mismatches)


def test_done_gate_mismatches_rejects_failed_check() -> None:
    done_gate = _matching_done_gate()
    done_gate["checks"][0]["passed"] = False  # type: ignore[index]
    mismatches = gate._done_gate_mismatches({"done_gate": done_gate})
    assert any("not all passed" in item for item in mismatches)


def test_done_gate_mismatches_rejects_missing_required_check() -> None:
    done_gate = _matching_done_gate()
    done_gate["checks"] = done_gate["checks"][:-1]  # type: ignore[assignment]
    mismatches = gate._done_gate_mismatches({"done_gate": done_gate})
    assert any("missing required items" in item for item in mismatches)


def test_done_gate_mismatches_rejects_non_true_verdict() -> None:
    mismatches = gate._done_gate_mismatches(
        {"done_gate": {"passed": "True", "checks": _matching_done_gate()["checks"]}}
    )
    assert any("must be true" in item for item in mismatches)


def test_done_gate_mismatches_rejects_malformed_check() -> None:
    done_gate = _matching_done_gate()
    done_gate["checks"] = [{"name": 42, "passed": True}]  # type: ignore[list-item]
    mismatches = gate._done_gate_mismatches({"done_gate": done_gate})
    assert any("malformed" in item for item in mismatches)
    assert any("missing required items" in item for item in mismatches)


def test_done_gate_mismatches_accepts_complete_passing_report() -> None:
    assert gate._done_gate_mismatches({"done_gate": _matching_done_gate()}) == []


def test_layer5_fails_on_empty_scopes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 counter-example: a certified canary with no scopes can never pass —
    the layer must not trust the process exit code."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(
        gate,
        "_run",
        lambda cmd, **kwargs: (0, ""),
    )
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps({"passed": True, "scopes": [], "companion_status": {}}),
        encoding="utf-8",
    )
    assert gate.layer5_real_canary(staging_dir).passed is False


def test_layer5_fails_on_missing_certified_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 counter-example: a canary omitting one certified scope fails even
    when every listed scope passes."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    canary = _matching_canary()
    canary["scopes"] = canary["scopes"][:-1]  # type: ignore[assignment]
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    assert gate.layer5_real_canary(staging_dir).passed is False


def test_layer5_fails_on_stale_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 counter-example: a certified scope that is stale (not fresh) fails."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    canary = _matching_canary()
    canary["scopes"][0]["fresh"] = False  # type: ignore[index]
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    assert gate.layer5_real_canary(staging_dir).passed is False


def test_layer5_fails_when_top_level_passed_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 counter-example: the canary JSON reporting passed=false must fail the
    layer even if the subprocess exits 0."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    canary = _matching_canary()
    canary["passed"] = False
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    assert gate.layer5_real_canary(staging_dir).passed is False


def test_layer5_passes_only_complete_certified_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 positive: the full certified scope set, each fresh/authorized/
    read-only/passed, passes the layer."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(_matching_canary()), encoding="utf-8"
    )
    assert gate.layer5_real_canary(staging_dir).passed is True


def test_layer3_browser_e2e_exit2_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-114 R3 counter-example: a clean-Chrome browser E2E that skips (exit 2)
    must NOT pass layer 3 — only a real rendered E2E in clean headless Chrome
    (exit 0) satisfies the fixture.  Skipping is honest reporting, not a pass."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        if any("scripts/browser_e2e.py" in part for part in cmd):
            return 2, "SKIP: no Google Chrome / Chromium binary found"
        return 0, ""

    monkeypatch.setattr(gate, "_run", fake_run)
    result = gate.layer3_clean_chrome_fixtures(staging_dir)
    assert result.passed is False
    assert not any(
        check["passed"]
        for check in result.sub_checks
        if check["name"] == "clean_chrome_browser_e2e"
    )


def test_layer3_browser_e2e_real_run_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-114 R3 positive: the clean-Chrome browser E2E that actually renders
    (exit 0) passes layer 3."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        if any("scripts/browser_e2e.py" in part for part in cmd):
            return 0, ""
        return 0, ""

    monkeypatch.setattr(gate, "_run", fake_run)
    result = gate.layer3_clean_chrome_fixtures(staging_dir)
    assert result.passed is True


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


def _inject_git_failure(
    monkeypatch: pytest.MonkeyPatch,
    subcmd: str,
    when: int | None = None,
    stderr: str = "injected failure",
) -> None:
    """Make ``gate._git`` fail on the ``when``-th (1-based) invocation of
    ``subcmd`` (``when=None`` → the first invocation).  Failures surface exactly
    as a real non-zero git exit would: ``check=True`` callers raise
    ``GateStateChangedError``, ``check=False`` callers get a non-zero proc."""

    real_git = gate._git
    seen = 0

    def fake_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        nonlocal seen
        if args and args[0] == subcmd:
            seen += 1
            if when is None or seen == when:
                proc = subprocess.CompletedProcess(
                    args, returncode=1, stdout=b"", stderr=stderr.encode()
                )
                if kwargs.get("check"):
                    raise gate.GateStateChangedError(
                        f"git {' '.join(args)} failed with exit 1: {stderr}"
                    )
                return proc
        return real_git(*args, **kwargs)

    monkeypatch.setattr(gate, "_git", fake_git)


def test_main_exits_2_when_evidence_commit_fails(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Real git lifecycle counter-example: a failed phase-1 ``commit-tree``
    during the evidence-commit phase must abort the gate with exit 2 — a commit
    failure must never be swallowed and reported as a pass, and the branch must
    stay on the tested revision."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    _inject_git_failure(monkeypatch, "commit-tree", when=1)

    head_before = _head(clean_repo)
    rc = gate.main(
        ["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"]
    )

    assert rc == 2
    assert _head(clean_repo) == head_before
    assert _porcelain(clean_repo) == ""


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
        "done_gate": _matching_done_gate(),
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


def test_main_exits_2_when_staging_is_non_empty_dir(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """C-114 R3 counter-example: a ``--staging-dir`` that already exists and is
    non-empty must be rejected with exit 2 — the gate never sweeps stale files
    from an earlier run into this run's evidence trail."""
    _patch_root(monkeypatch, clean_repo)
    before = _porcelain(clean_repo)
    stale = tmp_path / "staging-reused"
    stale.mkdir()
    (stale / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    rc = gate.main(["--staging-dir", str(stale), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before
    # The stale evidence was not touched.
    assert (stale / "product-acceptance.json").read_text() == '{"passed": true}\n'


def test_main_accepts_existing_empty_staging_dir(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """C-114 R3: an existing-but-empty ``--staging-dir`` is acceptable (the gate
    still populates it itself); only non-empty reuse is refused."""
    _patch_root(monkeypatch, clean_repo)
    empty = tmp_path / "staging-empty"
    empty.mkdir()
    _populating_passing_layers(monkeypatch, empty)
    rc = gate.main(["--staging-dir", str(empty), "--quiet"])
    assert rc == 0
    assert (empty / "product-v1-done-gate.json").is_file()


def test_new_staging_dir_embeds_unique_run_id() -> None:
    """C-114 R3: the default staging path embeds a per-run id, and two runs never
    collide on the same directory."""
    first = gate._new_staging_dir()
    second = gate._new_staging_dir()
    assert first != second
    assert first.parent == second.parent
    assert gate._RUN_ID_RE.search(first.name) is not None
    assert gate._RUN_ID_RE.search(second.name) is not None


def test_run_gate_report_carries_run_id(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-114 R3: the report binds the run_id supplied by main so the evidence
    trail identifies exactly one execution."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    staging = clean_repo.parent / "staging"
    report = gate.run_gate(staging, run_id="test-run-123")
    assert report.run_id == "test-run-123"


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
    """Counter-example: a real git failure (an existing .git/index.lock) must
    abort the phase, and the working tree must be rolled back so no passed=true
    report without an evidence commit is left on disk.

    With the temp ``GIT_INDEX_FILE`` staging, the real-index sync right before
    the atomic CAS is what the lock blocks — a real, unmonkeypatched git
    failure that still exercises the fail-closed rollback."""
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
    _populating_passing_layers(monkeypatch, staging_dir)
    # Fail the phase-2 pointer commit-tree: E exists as an object but the
    # branch must never advance to it, and the report on disk must say passed.
    _inject_git_failure(monkeypatch, "commit-tree", when=2)

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
    """Counter-example: a real git failure (an existing .git/index.lock blocking
    the pre-CAS real-index sync) during the evidence commit must roll the
    working tree back cleanly even when a binary PNG is among the written
    evidence — the rollback must not crash on the binary blob."""
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
    _populating_passing_layers(monkeypatch, staging_dir)
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )

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
    _populating_passing_layers(monkeypatch, staging_dir)
    monkeypatch.setattr(
        gate,
        "layer6_full_e2e",
        lambda *args, **kwargs: gate.LayerResult(
            name="6_full_e2e", passed=False, detail="real e2e failure"
        ),
    )

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
        json.dumps(_matching_canary()), encoding="utf-8"
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
    # This test exercises token-via-env propagation, not the R7 lease preflight;
    # isolate the preflight to a clean live state.
    monkeypatch.setattr(gate, "_live_state_lease_preflight", lambda *a, **k: [])
    start = _expected_snapshot(clean_repo)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(
            {"run_status": "completed", "done_gate": _matching_done_gate()}
        ),
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


# ---------------------------------------------------------------------------
# C-114 R7: read-only live-state lease preflight (residual queued/claimed
# leases must not pollute a new live run)
# ---------------------------------------------------------------------------


def _make_jobs_db(
    db_path: Path, rows: list[tuple[str, str, str | None]]
) -> None:
    """Create a minimal durable live-state ``jobs`` table matching the API's
    JobRow schema columns the preflight reads, seeded with
    (id, status, lease_expires_at) rows."""
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "CREATE TABLE jobs ("
        " id TEXT PRIMARY KEY,"
        " workspace_id TEXT,"
        " status TEXT,"
        " stage TEXT,"
        " progress INTEGER,"
        " attempts INTEGER,"
        " max_attempts INTEGER,"
        " lease_expires_at TEXT,"
        " idempotency_key TEXT,"
        " trace_id TEXT,"
        " request TEXT,"
        " result TEXT,"
        " error TEXT,"
        " created_at TEXT,"
        " updated_at TEXT"
        ")"
    )
    for job_id, status, lease in rows:
        connection.execute(
            "INSERT INTO jobs (id, status, lease_expires_at) VALUES (?, ?, ?)",
            (job_id, status, lease),
        )
    connection.commit()
    connection.close()


def _db_sha256(db_path: Path) -> str:
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


def _future_lease() -> str:
    return (datetime.now(UTC) + timedelta(minutes=15)).isoformat()


def _expired_lease() -> str:
    return (datetime.now(UTC) - timedelta(minutes=15)).isoformat()


def test_live_state_lease_preflight_detects_residual_queued_lease(
    tmp_path: Path,
) -> None:
    """R7 counter-example: a queued job with an unexpired lease is residual and
    must block a fresh run."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(
        db_path,
        [
            ("job-1", "queued", _future_lease()),
        ],
    )
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "job-1" in residual[0]
    assert "queued" in residual[0]


def test_live_state_lease_preflight_detects_residual_claimed_lease(
    tmp_path: Path,
) -> None:
    """R7 counter-example: a claimed (running) job with an unexpired lease is
    residual."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(
        db_path,
        [
            ("job-2", "running", _future_lease()),
        ],
    )
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "job-2" in residual[0]
    assert "running" in residual[0]


def test_live_state_lease_preflight_detects_queued_without_lease(
    tmp_path: Path,
) -> None:
    """R7 counter-example: a queued job with no lease at all is still pending
    work a fresh run would race with."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [("job-3", "queued", None)])
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "job-3" in residual[0]


def test_live_state_lease_preflight_passes_on_clean_db(tmp_path: Path) -> None:
    """R7 positive: an empty jobs table is isolated."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [])
    assert gate._live_state_lease_preflight(db_path) == []


def test_live_state_lease_preflight_ignores_expired_leases(
    tmp_path: Path,
) -> None:
    """R7 positive: an expired lease has lapsed and cannot contaminate a new
    run — only unexpired queued/claimed leases are residual."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(
        db_path,
        [
            ("job-old", "running", _expired_lease()),
            ("job-done", "succeeded", _future_lease()),
            ("job-failed", "failed", _future_lease()),
        ],
    )
    assert gate._live_state_lease_preflight(db_path) == []


def test_live_state_lease_preflight_fails_closed_on_missing_db(
    tmp_path: Path,
) -> None:
    """R7 counter-example: a missing live-state DB cannot prove lease isolation
    and must fail closed."""
    db_path = tmp_path / "does-not-exist.db"
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "missing" in residual[0]


def test_live_state_lease_preflight_is_strictly_read_only(tmp_path: Path) -> None:
    """R7 safety: the preflight opens the DB ``mode=ro`` — it must neither clear
    nor extend a lease, nor create a journal/WAL.  Bytes before == bytes after,
    and no sidecar journal file may appear."""
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [("job-4", "running", _future_lease())])
    before = _db_sha256(db_path)
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    after = _db_sha256(db_path)
    assert before == after
    assert not (tmp_path / "live.db-journal").exists()
    assert not (tmp_path / "live.db-wal").exists()


def test_resolve_live_state_db_honors_explicit_and_local_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R7 wiring: an explicit ``--live-state-db`` wins; otherwise a local sqlite
    DATABASE_URL path is honoured; otherwise the repo-root default applies."""
    explicit = tmp_path / "explicit.db"
    assert gate._resolve_live_state_db(explicit) == explicit
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./custom.db")
    assert gate._resolve_live_state_db().name == "custom.db"
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("TRIPCHORD_DATABASE_URL", "sqlite+aiosqlite:///./custom2.db")
    assert gate._resolve_live_state_db().name == "custom2.db"
    monkeypatch.delenv("TRIPCHORD_DATABASE_URL")
    assert gate._resolve_live_state_db() == gate.ROOT / "tripchord.db"


def test_layer6_fails_when_residual_lease_present(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    tmp_path: Path,
    staging_dir: Path,
) -> None:
    """R7 integration: layer 6 refuses to run a fresh E2E when the live-state DB
    holds a residual queued/claimed lease, even when every auth gate is met."""
    staging_dir.mkdir()
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [("job-5", "queued", _future_lease())])
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")
    start = _expected_snapshot(clean_repo)
    result = gate.layer6_full_e2e(staging_dir, start, live_state_db=db_path)
    assert result.passed is False
    assert "lease preflight" in result.detail


def test_layer6_lease_preflight_passes_on_clean_live_state(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    tmp_path: Path,
    staging_dir: Path,
) -> None:
    """R7 integration: a clean live-state DB passes the preflight and the layer
    proceeds to the E2E runner (which then drives the verdict)."""
    staging_dir.mkdir()
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [])
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    monkeypatch.setattr(gate, "_runner_revision_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_runtime_provenance_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_extract_build_fingerprint", lambda *a, **k: None)
    start = _expected_snapshot(clean_repo)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"run_status": "completed", "done_gate": _matching_done_gate()}),
        encoding="utf-8",
    )
    result = gate.layer6_full_e2e(staging_dir, start, live_state_db=db_path)
    assert result.passed is True


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
    """The required-input verifier accepts a complete staging set (raw evidence
    plus the derived layer-5/6 compact artifacts) and fails closed on any
    missing member."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    gate._generate_compact_evidence(staging_dir)
    gate._verify_required_evidence_inputs(staging_dir)  # no raise

    (staging_dir / "browser-e2e.json").unlink()
    with pytest.raises(gate.GateStateChangedError, match="browser-e2e.json"):
        gate._verify_required_evidence_inputs(staging_dir)

    # The compact artifacts are contract-required too (C-114): a staging set
    # missing a layer-5/6 compact must fail the gate, not just the raw file.
    (staging_dir / gate._COMPACT_E2E_STAGED_NAME).unlink()
    with pytest.raises(
        gate.GateStateChangedError, match=gate._COMPACT_E2E_STAGED_NAME
    ):
        gate._verify_required_evidence_inputs(staging_dir)


def test_compact_canary_drops_raw_detail_fields(staging_dir: Path) -> None:
    """C-114 counter-example: the desensitized layer-5 compact artifact must
    carry only the safe structured verdict fields, never free-text detail or
    raw detail/URL bytes that could hide account/session material."""
    staging_dir.mkdir()
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(
            {
                "passed": True,
                "bridge_token_present": True,
                "generated_at": "2026-08-10T00:00:00+00:00",
                "scopes": [
                    {
                        "scope": "ctrip:flight",
                        "kind": "ota",
                        "passed": True,
                        "fresh": True,
                        "authorized": True,
                        "read_only": True,
                        "detail": "https://flights.ctrip.com/online/list?sid=SESSIONVALUE123",
                    }
                ],
                "companion_status": {
                    "status": "ok",
                    "stale_after_seconds": 30,
                    "companions": [
                        {
                            "companion_id": "comp-1",
                            "providers": ["p"],
                            "authorized_scope_keys": ["x"],
                            "is_fresh": True,
                            "age_seconds": 3,
                            "build_identity": {"build_sha256": "abc123"},
                        }
                    ],
                },
                "extra_raw_field": "anything",
            }
        ),
        encoding="utf-8",
    )
    compact = gate._compact_canary(staging_dir)
    assert compact is not None
    assert compact["scopes"][0]["scope"] == "ctrip:flight"
    assert compact["scopes"][0]["passed"] is True
    # Free-text detail / raw extra fields are never copied into the compact.
    assert "detail" not in compact["scopes"][0]
    assert "extra_raw_field" not in compact
    assert "sid=" not in json.dumps(compact)
    assert "SESSIONVALUE123" not in json.dumps(compact)
    assert compact["raw_evidence"]["file"] == "live-canary-certified.json"
    assert compact["raw_evidence"]["committed"] is False


def test_compact_canary_carries_coverage_and_bindings(staging_dir: Path) -> None:
    """C-114 R5: the layer-5 compact must carry the certified-scope coverage
    thresholds and the per-scope provider/query/candidate/quote bindings — not
    just a pass/fail boolean — so a reviewer can independently verify the canary
    actually exercised every declared scope."""
    staging_dir.mkdir()
    canary = dict(_matching_canary())
    canary["scopes"] = [
        {
            "scope": "ctrip:flight",
            "kind": "companion_heartbeat",
            "passed": True,
            "fresh": True,
            "authorized": True,
            "read_only": True,
            "evidence": {
                "companion_id": "companion-001",
                "providers": ["ctrip"],
                "authorized_scope_keys": ["ctrip:flight"],
                "runtime_instance_id": "inst-abc",
                "adapter_version": "v2",
            },
        },
        {
            "scope": "icom:transfer",
            "kind": "icom_public_api",
            "passed": True,
            "fresh": True,
            "authorized": True,
            "read_only": True,
            "evidence": {
                "searched_at": "2026-08-10T00:00:00Z",
                "source_urls": [
                    "https://transfer.example/search?from=MLE",
                    "https://transfer.example/search?from=MLE&to=AIR",
                ],
                "options": 7,
                "sample": {
                    "service_name": "speedboat",
                    "fare_amount": "42.00",
                    "currency": "USD",
                },
            },
        },
    ]
    canary["passed"] = False  # only 2 of the 6 certified scopes are present
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    compact = gate._compact_canary(staging_dir)
    assert compact is not None
    coverage = compact["coverage"]
    assert coverage["expected_scope_count"] == 6
    assert set(coverage["expected_scopes"]) == set(gate._CERTIFIED_OTA_SCOPES)
    assert coverage["passed_scope_count"] == 2
    assert "qunar:lodging" in coverage["missing"]

    by_scope = {entry["scope"]: entry for entry in compact["scopes"]}
    ctrip = by_scope["ctrip:flight"]
    assert ctrip["provider"] == "ctrip"
    assert ctrip["evidence"]["companion_id"] == "companion-001"
    assert ctrip["evidence"]["providers"] == ["ctrip"]
    icom = by_scope["icom:transfer"]
    assert icom["provider"] == "icom"
    assert icom["evidence"]["options"] == 7
    assert icom["evidence"]["sample"]["fare_amount"] == "42.00"
    # Raw URLs are never committed into the compact.
    assert "source_urls" not in icom["evidence"]
    assert "search?from=" not in json.dumps(compact)


def test_compact_live_e2e_carries_15_checks(staging_dir: Path) -> None:
    """C-114 R5: the layer-6 compact must carry the full 15-item done-gate check
    set (including the planner-verifier-repair chain, exact budget + selected
    evidence, and the event-injection repair/re-verify master) so a reviewer can
    re-verify each verdict without the raw runner payload."""
    staging_dir.mkdir()
    done_gate = _matching_done_gate()
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(
            {
                "schema_version": "tripchord-live-v4-done-gate-report",
                "run_status": "completed",
                "repo_revision": "abc123",
                "captured_at": "2026-08-10T00:00:00Z",
                "done_gate": done_gate,
            }
        ),
        encoding="utf-8",
    )
    compact = gate._compact_live_e2e(staging_dir)
    assert compact is not None
    assert compact["done_gate"]["check_count"] == 15
    assert compact["done_gate"]["passed_check_count"] == 15
    names = [check["name"] for check in compact["done_gate"]["checks"]]
    for required in (
        "planner_verifier_repair_master_stay_plan_chain",
        "planner_verifier_repair_orchestrator",
        "exact_budget_and_selected_evidence",
        "event_injection_repair_reverify_master",
    ):
        assert required in names
    assert compact["done_gate"]["checks"][0]["summary"] == "ok"


def test_verify_evidence_contract_rejects_blank_compact_content(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 R5 counter-example: a committed compact blob that parses but lacks
    the contract-required content (layer-5 coverage / layer-6 check set) must
    fail the post-commit re-verify — a blank or hash-only artifact is not enough."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    blank_payload = '{"schema_version": "tripchord-done-gate-layer6-compact-v2"}\n'
    staged_compact = staging_dir / gate._COMPACT_E2E_STAGED_NAME
    staged_compact.write_text(blank_payload, encoding="utf-8")
    manifest = {
        "schema_version": "tripchord-product-v1-done-gate-manifest",
        "tested_commit_sha": _head(clean_repo),
        "run_id": "test-run",
        "evidence_commit": _head(clean_repo),
        "generated_at": "2026-08-10T00:00:00+00:00",
        "toplevel": str(clean_repo),
        "branch": "main",
        "files": [
            {
                "name": gate._COMPACT_E2E_STAGED_NAME,
                "tracked_path": gate._EVIDENCE_TRACKED_PATHS[-1][1],
                "sha256": gate._sha256_file(staged_compact),
                "size_bytes": len(blank_payload),
                "committed": True,
            }
        ],
        "layer_verdicts": {"5_real_canary": {}, "6_full_e2e": {}},
    }
    # E (== HEAD) carries the manifest plus a compact artifact with no done-gate
    # check set.
    results = clean_repo / "benchmarks" / "results"
    results.mkdir(parents=True, exist_ok=True)
    rel = gate._EVIDENCE_TRACKED_PATHS[-1][1]
    blank = results / Path(rel).name
    blank.write_text(blank_payload, encoding="utf-8")
    (results / Path(gate._MANIFEST_REL).name).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    gate._git("add", "--", rel, gate._MANIFEST_REL, check=True)
    gate._git(
        "commit",
        "-q",
        "-m",
        "blank compact",
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="done-gate check set"):
        gate._verify_evidence_contract(_head(clean_repo), staging_dir, manifest)


def test_main_commits_desensitized_layer5_6_compact_artifacts(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 evidence-contract counter-example: the committed trail must carry
    independently reviewable desensitized layer-5/6 compact artifacts — never
    only a committed=false raw hash — and E must contain them."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    # Reproduce the real repository's rule: raw live-* evidence is gitignored,
    # so only the compact artifacts can (and must) be committed.
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])
    assert rc == 0

    head = _head(clean_repo)
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", head],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in (
        "benchmarks/results/done-gate-layer5-compact.json",
        "benchmarks/results/done-gate-layer6-compact.json",
    ):
        assert rel in tree, f"compact artifact missing from committed tree: {rel}"
    manifest = json.loads(
        (clean_repo / gate._MANIFEST_REL).read_text(encoding="utf-8")
    )
    by_name = {entry["name"]: entry for entry in manifest["files"]}
    assert by_name[gate._COMPACT_CANARY_STAGED_NAME]["committed"] is True
    assert by_name[gate._COMPACT_E2E_STAGED_NAME]["committed"] is True
    # The raw layer-5/6 files are still not committed (gitignored).
    assert by_name["live-canary-certified.json"]["committed"] is False
    assert by_name["live-done-gate-v4.json"]["committed"] is False
    # The compact artifacts are independently reviewable structured JSON.
    compact = json.loads(
        (
            clean_repo
            / "benchmarks"
            / "results"
            / "done-gate-layer5-compact.json"
        ).read_text(encoding="utf-8")
    )
    assert compact["schema_version"].startswith("tripchord-done-gate-layer5")
    assert _porcelain(clean_repo) == ""


def test_main_exits_2_when_compact_artifact_missing(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 evidence-contract counter-example: a passing run whose compact
    artifact is absent (as if compact generation was skipped) must exit 2 at the
    contract gate and produce no commit — a committed=false raw hash alone is
    never enough layer-5/6 evidence."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _populate_required_evidence(staging_dir)
    # Simulate compact generation dropping the layer-6 compact.
    real_generate = gate._generate_compact_evidence

    def generate_without_e2e_compact(staging: Path) -> None:
        real_generate(staging)
        (staging / gate._COMPACT_E2E_STAGED_NAME).unlink()

    monkeypatch.setattr(gate, "_generate_compact_evidence", generate_without_e2e_compact)

    head_before = _head(clean_repo)
    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])
    assert rc == 2
    assert _head(clean_repo) == head_before
    assert _porcelain(clean_repo) == ""


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


# ---------------------------------------------------------------------------
# Evidence disk safety (A3: 0700/0600, symlink/hardlink/owner, multi-class scan)
# ---------------------------------------------------------------------------


def _staging_evidence(staging_dir: Path) -> None:
    """Minimal staging set for a passing run (no bridge token configured)."""
    staging_dir.mkdir(exist_ok=True)
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"run_status": "completed"}\n', encoding="utf-8"
    )


def test_harden_staging_permissions_sets_0700_0600(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: after the gate runs, the staging dir must be 0700 and
    every raw evidence file 0600 — never world/group readable."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    rc = gate.main(["--staging-dir", str(staging_dir), "--quiet"])
    assert rc == 0
    assert (staging_dir.stat().st_mode & 0o777) == 0o700
    for name in ("product-acceptance.json", "live-done-gate-v4.json"):
        assert (staging_dir / name).stat().st_mode & 0o777 == 0o600


def test_secret_scan_rejects_symlink_in_staging(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a symlink planted in staging must fail the gate —
    it could redirect the evidence read to attacker-chosen bytes."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    target = staging_dir.parent / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (staging_dir / "live-canary-certified.json").symlink_to(target)

    with pytest.raises(gate.GateStateChangedError, match="symlink"):
        gate.run_gate(staging_dir)


def test_secret_scan_rejects_hardlink_in_staging(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a hardlink planted in staging must fail the gate."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    src = staging_dir / "live-canary-certified.json"
    src.write_text('{"scopes": []}\n', encoding="utf-8")
    os.link(src, staging_dir / "browser-e2e.json")

    with pytest.raises(gate.GateStateChangedError, match="hardlink"):
        gate.run_gate(staging_dir)


def test_secret_scan_rejects_non_current_user_file(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a staging file owned by a different uid must fail the
    gate even when nothing else is wrong with it."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    with pytest.raises(gate.GateStateChangedError, match="owned by uid"):
        gate.run_gate(staging_dir)


def test_secret_scan_fails_closed_on_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 R4 counter-example: a mode-000 evidence file that cannot be read
    must fail the scan (never silently pass) — an unreadable file could hide a
    secret.  The error names only the category and file, not content."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    unreadable = staging_dir / "product-acceptance.json"
    unreadable.chmod(0o000)
    try:
        with pytest.raises(
            gate.GateStateChangedError, match="cannot read evidence file"
        ):
            gate._secret_scan_staging(
                staging_dir, ("nope",)
            )
    finally:
        unreadable.chmod(0o600)


def test_secret_scan_covers_all_model_api_key_envs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-114 R4 counter-example: the scan must cover EVERY model provider key the
    host exports — OPENAI_API_KEY / ANTHROPIC_API_KEY included — not just the
    primary MODEL_API_KEY."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-abc123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz789")
    secrets = gate._evidence_secrets()
    assert "sk-openai-abc123" in secrets
    assert "sk-ant-xyz789" in secrets

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "evidence.json").write_text(
        '{"key": "sk-ant-xyz789"}\n', encoding="utf-8"
    )
    with pytest.raises(gate.GateStateChangedError, match="secret value found"):
        gate._secret_scan_staging(staging_dir, secrets)


def test_harden_staging_rejects_symlink_subdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-114 R6 counter-example: a symlink planted as a staging subdirectory must
    be rejected by lstat BEFORE its chmod — chmod would otherwise follow the link
    onto the attacker-chosen target."""
    _patch_root(monkeypatch, tmp_path / "repo")
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "real").mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (staging_dir / "evil").symlink_to(outside)

    with pytest.raises(gate.GateStateChangedError, match="symlink"):
        gate._harden_staging_permissions(staging_dir)


def test_harden_staging_rejects_symlink_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-114 R6 counter-example: a staging ROOT that is a symlink must be
    rejected before chmod — chmod(0o700) on a symlink would hit its target."""
    _patch_root(monkeypatch, tmp_path / "repo")
    real_dir = tmp_path / "real-staging"
    real_dir.mkdir()
    staging_dir = tmp_path / "staging-link"
    staging_dir.symlink_to(real_dir)

    with pytest.raises(gate.GateStateChangedError, match="symlink"):
        gate._harden_staging_permissions(staging_dir)


def test_dump_output_atomic_0600_outside_staging(tmp_path: Path) -> None:
    """C-114 R6 counter-example: ``--output`` outside the staging tree must still
    be 0600 after the atomic rename — the host umask must not widen the report."""
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha="a" * 40,
        run_id="test-run",
        passed=False,
        summary="x",
    )
    out = tmp_path / "elsewhere" / "report.json"
    gate._dump(report, out)
    assert out.exists()
    assert out.stat().st_mode & 0o777 == 0o600


def test_secret_scan_flags_tracking_url_query(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a full tracking URL with a live query value in raw
    evidence must fail the gate — a byte scan of the token alone cannot see it."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"result": {"source_urls": [
            "https://flights.ctrip.com/online/list?sid=AbCdEfGh123456"
        ]}}),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="tracking URL"):
        gate.run_gate(staging_dir)


def test_secret_scan_allows_public_api_date_query(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 negative: a public read-only API URL carrying a plain ISO-8601 date
    (e.g. a schedules ``?date=2026-08-13``) is NOT a tracking URL — the scan
    must not mistake the 4-digit year for an opaque numeric token."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"result": {"source_urls": [
            "https://sfs-api.icomtours.com/api/v1/public/trips/"
            "schedules?date=2026-08-13",
            "https://sfs-api.icomtours.com/api/v1/public/trips/"
            "schedules?from=2026-08-13T07%3A30%3A00%2B05%3A00&to=2026-08-13",
        ]}}),
        encoding="utf-8",
    )
    # Must not raise: no session/account token, no Authorization/Cookie, no
    # bare phone number and no 16+ char opaque value.
    gate.run_gate(staging_dir)


def test_secret_scan_flags_authorization_header(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: an Authorization header value in evidence fails the
    gate even when the bridge token itself is absent."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"request": {"headers": "Authorization: Bearer '
        'sk-abcdefghijklmnopqrstuvwxyz123456"}}',
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
        gate.run_gate(staging_dir)


def test_secret_scan_flags_cookie_header(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example (C-114): a Cookie header value in evidence fails the
    gate even when the bridge token itself is absent and the value never
    appears as raw bytes of an active secret."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"trace": {"headers": "Cookie: '
        'JSESSIONID=abcdefghijklmnopqrstuvwxyz123456"}}',
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
        gate.run_gate(staging_dir)


def test_secret_scan_flags_account_identifier(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a numeric account identifier in evidence fails the
    gate."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"trace": {"account_id": 12345678}}), encoding="utf-8"
    )
    with pytest.raises(gate.GateStateChangedError, match="account identifier"):
        gate.run_gate(staging_dir)


def test_secret_scan_flags_model_api_key_from_env(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: a model API key configured on the host that leaks
    into evidence fails the gate even when the bridge token is unset."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    key = "sk-prod-0123456789abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("MODEL_API_KEY", key)
    (staging_dir / "product-acceptance.json").write_text(
        json.dumps({"leak": key}), encoding="utf-8"
    )
    with pytest.raises(gate.GateStateChangedError, match="secret value"):
        gate.run_gate(staging_dir)


def test_secret_scan_allows_redacted_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A3 counter-example: desensitized evidence — [REDACTED] values, benign
    query params, no account identifiers — must pass the scan untouched."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(
            {
                "run_status": "completed",
                "result": {
                    "source_urls": [
                        "https://example.com/search?q=hotel&page=1",
                        "https://example.com/pay?sid=[REDACTED]",
                    ]
                },
                "runtime_provenance": {"pid": 1234, "commit_sha": "a" * 40},
            }
        ),
        encoding="utf-8",
    )
    gate.run_gate(staging_dir)  # no raise


def test_commit_evidence_catches_last_step_report_leak(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 counter-example: a secret that leaks only into the report written
    at the very end of the evidence phase must abort the commit before the CAS.

    run_gate's staging scan runs before the report exists, so only the final
    comprehensive scan (which runs after every report/manifest/compact write and
    before the atomic ref update) can see the leak — the ordering fix that keeps
    a passed=true report with a leaked secret from ever being committed."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    token = "F" * 64
    monkeypatch.setattr(gate, "_bridge_token", lambda: token)
    monkeypatch.setattr(
        gate,
        "layer6_full_e2e",
        lambda *args, **kwargs: gate.LayerResult(
            name="6_full_e2e", passed=True, detail=token
        ),
    )

    head_before = _head(clean_repo)
    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 2
    assert _head(clean_repo) == head_before
    assert _porcelain(clean_repo) == ""
    # The delivered report on disk must carry the voided verdict, never the leak.
    payload = json.loads(
        (staging_dir / "product-v1-done-gate.json").read_text(encoding="utf-8")
    )
    assert payload["passed"] is False
    assert payload["evidence_commit"] is None


# ---------------------------------------------------------------------------
# Two-phase evidence-commit atomicity (A4: atomic ref update, no intermediate E)
# ---------------------------------------------------------------------------


def _passing_report_and_start(clean_repo: Path) -> tuple[gate.GateReport, gate.GitSnapshot]:
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
    return report, _expected_snapshot(clean_repo)


def _assert_phase_failure_is_atomic(
    clean_repo: Path, staging_dir: Path, tested_sha: str
) -> None:
    """After any phase-1/phase-2/add/update-ref failure the branch must still be
    at the tested revision, the object graph may not expose an intermediate E on
    the branch, and the index + worktree must be byte-for-byte clean."""
    assert _head(clean_repo) == tested_sha, "branch moved on a failed commit phase"
    assert _porcelain(clean_repo) == "", "failed commit phase left a dirty tree"
    # No intermediate commit may be reachable from the branch tip.
    log = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--oneline", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Done-Gate evidence" not in log
    assert "evidence_commit=" not in log


def test_commit_evidence_phase1_add_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed phase-1 ``git add`` leaves the branch on the
    tested revision, no intermediate commit, and a clean index/worktree."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "add", when=1)

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_phase1_commit_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed phase-1 ``commit-tree`` leaves the branch on
    the tested revision — E is never installed as HEAD."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "commit-tree", when=1)

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_phase2_commit_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed phase-2 ``commit-tree`` (after E was
    materialized) leaves the branch on the tested revision — E is never
    installed, so no intermediate commit pollutes the branch history."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "commit-tree", when=2)

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_update_ref_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed compare-and-swap ``update-ref`` (the atomic
    commit point) leaves the branch on the tested revision even though E and P
    were fully materialized — the branch only advances atomically or not at
    all."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "update-ref")

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_success_moves_head_once_atomically(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 positive control: on success HEAD lands exactly on the pointer commit
    P whose parent is E, E's parent is the tested revision, and the tree is
    clean — the atomic trail S -> E -> P is exactly what update-ref installed."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    head = _head(clean_repo)
    assert head != start.commit_sha
    # P's parent is E, E's parent is S.
    parent_p = subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", f"{head}^"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert parent_p == evidence_commit
    parent_e = subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", f"{evidence_commit}^"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert parent_e == start.commit_sha
    assert _porcelain(clean_repo) == ""
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["evidence_commit"] == evidence_commit
    assert payload["passed"] is True


def test_commit_evidence_no_fallible_op_after_cas(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 counter-example (CAS 成功后检查异常): after the atomic update-ref
    CAS succeeds there must be NO fallible operation left.

    The old tail called ``_git_snapshot()`` and ran fail-able checks after the
    CAS — a failure there would raise, the rollback would then restore files to
    the already-installed pointer commit P, and the flow would report failure
    while a passed=true report was committed at P.  Injecting a failure on a
    second snapshot proves the tail check is gone: the gate commits cleanly and
    no post-CAS operation can flip success into a voided-but-installed state."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    real_snapshot = gate._git_snapshot
    seen = 0

    def fake_snapshot(*args: object, **kwargs: object) -> gate.GitSnapshot:
        nonlocal seen
        seen += 1
        if seen == 2:
            raise gate.GateStateChangedError(
                "post-CAS snapshot would fail (old tail check)"
            )
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(gate, "_git_snapshot", fake_snapshot)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The gate succeeded: HEAD advanced to P, the tree is clean, and the
    # committed report carries passed=true with the evidence trail.
    assert _head(clean_repo) != start.commit_sha
    assert _porcelain(clean_repo) == ""
    report_path = clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["evidence_commit"] == evidence_commit
    assert payload["passed"] is True


def test_commit_evidence_rollback_surfaces_reset_failure(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 counter-example: a failed rollback is never silently swallowed —
    the rollback reset must be check=True so a dirty index cannot be left behind
    while the gate reports only the original failure."""
    _patch_root(monkeypatch, clean_repo)
    _populate_required_evidence(staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "update-ref")

    real_git = gate._git
    seen_reset = 0

    def failing_reset(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        nonlocal seen_reset
        if args and args[0] == "reset":
            seen_reset += 1
            proc = subprocess.CompletedProcess(
                args, returncode=1, stdout=b"", stderr=b"reset failed"
            )
            if kwargs.get("check"):
                raise gate.GateStateChangedError("git reset failed with exit 1")
            return proc
        return real_git(*args, **kwargs)

    # Chain: _inject_git_failure handles the update-ref, this wrapper handles
    # the rollback reset.
    monkeypatch.setattr(gate, "_git", failing_reset)

    with pytest.raises(gate.GateStateChangedError, match="rollback also failed"):
        gate._commit_evidence(staging_dir, report, start=start)
    assert seen_reset >= 1
