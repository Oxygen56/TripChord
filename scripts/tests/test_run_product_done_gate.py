from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from tripchord.planning.frozen_graph import frozen_v4_pair_id_digest

from scripts import run_product_done_gate as gate

# C-122 P0 side-channel publish (2026-08-10 11:00): evidence is published through
# a namespaced ref ``refs/tripchord/done-gate/<run_id>`` created atomically at the
# very end, while the product branch / HEAD / real index / worktree stay
# byte-for-byte read-only.  A 12-hex run_id is required to name the ref.
_TEST_RUN_ID = "a1b2c3d4e5f6"


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
    for attr, layer_name in (
        ("layer1_reproducibility", "1_reproducibility"),
        ("layer2_replay", "2_replay"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
        ("layer6_full_e2e", "6_full_e2e"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name, **kwargs: gate.LayerResult(
                name=layer_name, passed=True
            ),
        )


def _populating_passing_layers(
    monkeypatch: pytest.MonkeyPatch, staging_dir: Path
) -> None:
    """Mock every layer to pass AND write the raw required evidence into staging,
    modelling the real flow (C-114 R3): main() creates an initially-empty staging
    dir, then the layers populate it — the tests never hand main() a pre-filled
    dir, because the gate refuses reused non-empty staging."""
    # A real runtime always has a persisted bridge-state file (docs/operations.md
    # pins TRIPCHORD_BROWSER_BRIDGE_STATE_PATH); simulate it so the layer-6
    # compact's bridge_state_lease_preflight binding carries a valid sha256
    # (C-122 Fix 2).  The file lives out-of-repo so it never dirties porcelain.
    bridge_state_path = staging_dir.parent / "bridge-state.json"
    bridge_state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(bridge_state_path))
    monkeypatch.setattr(
        gate,
        "layer1_reproducibility",
        lambda *args, sd=staging_dir: (
            _populate_required_evidence(sd),
            gate.LayerResult(name="1_reproducibility", passed=True),
        )[1],
    )
    for attr, layer_name in (
        ("layer2_replay", "2_replay"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
        ("layer6_full_e2e", "6_full_e2e"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name, **kwargs: gate.LayerResult(
                name=layer_name, passed=True
            ),
        )


def _populating_passing_layers_without(
    monkeypatch: pytest.MonkeyPatch, staging_dir: Path, *missing_names: str
) -> None:
    """Like ``_populating_passing_layers`` but a set of required evidence inputs
    is removed AFTER the layers write them, modelling a run that never produced
    those inputs (C-118: evidence-contract counter-examples)."""

    def layer1(
        *args: object,
        sd: Path = staging_dir,
        names: tuple[str, ...] = missing_names,
    ) -> gate.LayerResult:
        _populate_required_evidence(sd)
        for name in names:
            (sd / name).unlink(missing_ok=True)
        return gate.LayerResult(name="1_reproducibility", passed=True)

    monkeypatch.setattr(gate, "layer1_reproducibility", layer1)
    for attr, layer_name in (
        ("layer2_replay", "2_replay"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
        ("layer6_full_e2e", "6_full_e2e"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name, **kwargs: gate.LayerResult(
                name=layer_name, passed=True
            ),
        )


def _realistic_e2e_evidence() -> dict[str, object]:
    """A full passing layer-6 runner bundle that survives the strong compact
    contract (C-118): ``run_status`` completed, the 15-item ``done_gate`` all
    passed, and the repo / runtime / Companion identity plus the
    event-injection / timeout / runner contracts the committed layer-6 compact
    must preserve.  Values are synthetic and secret-free."""
    return {
        "schema_version": "tripchord-live-v4-done-gate-report",
        "run_status": "completed",
        "repo_revision": {
            "toplevel": "/repo",
            "branch": "main",
            "commit_sha": "a" * 40,
            "worktree_dirty": False,
        },
        "runtime_before_run": {
            "model_provider": "test-provider",
            "primary_model": "test-model",
            "model_enabled": True,
            "model_required": True,
            "runtime_provenance": {
                "repo_toplevel": "/repo",
                "commit_sha": "a" * 40,
                "dependency_lock_sha256": None,
                "live_system_source_sha256": None,
                "python_version": "3.12",
                "started_at": "2026-08-10T00:00:00+00:00",
            },
        },
        "companion_preflight": {
            "status": "connected",
            "stale_after_seconds": 45,
            "companions": [
                {
                    "companion_id": "comp-1",
                    "providers": ["ctrip", "qunar", "tongcheng"],
                    "is_fresh": True,
                    "age_seconds": 3,
                    # C-122 HG-A: exactly the six BROWSER Companion OTA scopes.
                    "authorized_scope_keys": sorted(gate._CERTIFIED_OTA_SCOPES),
                }
            ],
        },
        "done_gate": _matching_done_gate(),
        "api_payload_candidate_set_sha256": "a" * 64,
        # C-122 round-19 (gap 4): the raw request identity the compact builder
        # lifts into the layer-6 compact — the checkpoint binding's request SHA
        # is bound to this api_payload_sha256, so a foreign request-payload
        # binding fails closed.
        "request_identity": {
            "scenario_sha256": "a" * 64,
            "api_payload_sha256": _FIXTURE_REQUEST_SHA256,
            "digests_are_distinct_contracts": True,
        },
        "scenario_sha256": "a" * 64,
        "event_injection_contract": {
            "mode": "synthetic_sold_out_fault_injection",
            "source": "tripchord-done-gate-synthetic-fault",
            "platform_sold_out_observed": False,
            "platform_price_change_observed": False,
            "verified_change_scope": (
                "different_available_replacement_identity_not_platform_sold_out"
            ),
            "claim_boundary": "only_affected_component_recheck",
        },
        "timeout_contract": {
            "server_execution_timeout_seconds": 3600,
            "client_wait_timeout_seconds": 3600,
            "minimum_client_margin_seconds": 30,
        },
        "runner_contract": {
            "require_model_enhancement": True,
            "maximum_quote_age_minutes": 15,
            "minimum_recommendable_options": 2,
        },
        # C-122 supervision 01:10: the job control plane's record of the pairs
        # the run ACTUALLY sealed — the compact merges these into the
        # v4_source_graph evidence so the pair-id set is bound to an independent
        # checkpoint, not just the producer's own claim.  The fixture keeps the
        # bindings in agreement with the compact's pair_ids (all three canonical).
        # C-122 supervision 18:13 (Fix 4): the raw binding also carries the full
        # chain / dates / request / content so the compact can re-derive the
        # complete desensitized checkpoint binding.
        "context": {"pair_checkpoint_binding": _fixture_checkpoint_binding()},
    }


def _repo_head_or_none() -> str | None:
    """The HEAD sha of the patched repo root, or None when it is not a repo —
    the raw layer-6 fixture models the REAL flow where the runner snapshots the
    tested revision, so its compact must bind the repo that is actually
    committed (C-122 acceptance: repo == runtime == S)."""
    try:
        return gate._git("rev-parse", "HEAD", check=True).stdout.strip()
    except Exception:
        return None


def _populate_required_evidence(staging_dir: Path) -> None:
    """Write the fixed required raw-evidence inputs into staging so main()'s
    evidence-contract gate passes and the commit phase is actually exercised.

    The layer-5/6 raw files carry REALISTIC passing verdicts (C-118): the
    desensitized compact artifacts derived from them must survive the strong
    blob-read-back contract — the six registry-derived certified canary scopes
    (five browser Companion OTA + iCom public-API) each fresh/authorized/
    read-only/passed for layer 5, and the full fifteen done-gate checks all
    passed plus the
    repo/runtime/Companion identity for layer 6.
    """
    staging_dir.mkdir(exist_ok=True)
    (staging_dir / "product-acceptance.json").write_text(
        '{"passed": true}\n', encoding="utf-8"
    )
    (staging_dir / "browser-e2e.json").write_text('{"passed": true}\n', encoding="utf-8")
    (staging_dir / "browser-e2e-screenshot.png").write_bytes(b"PNG")
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(_matching_canary()), encoding="utf-8"
    )
    raw_e2e = _realistic_e2e_evidence()
    head = _repo_head_or_none()
    if head is not None:
        raw_e2e["repo_revision"]["commit_sha"] = head  # type: ignore[index]
        raw_e2e["runtime_before_run"]["runtime_provenance"]["commit_sha"] = head  # type: ignore[index]
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(raw_e2e), encoding="utf-8"
    )


def _populate_full_required_evidence(
    monkeypatch: pytest.MonkeyPatch, staging_dir: Path
) -> None:
    """Full canonical staging for a passing publish (C-122 round-19 02:56
    supervision / gap 3): every fixed evidence input — the raw layer-5/6
    evidence, the acceptance / e2e artifacts and the screenshot — plus the
    derived layer-5/6 compact artifacts.

    The manifest contract is the FIXED full evidence set and the publish
    preflight no longer derives the expected names from whatever staging
    happens to hold, so any commit-phase test must stage the complete set
    (including the compacts) or the publish fails closed.  A persisted
    bridge-state file is simulated so the layer-6 compact's bridge-state lease
    bindings carry a valid sha256 (C-122 Fix 2); the module-level bridge
    snapshots are cleared so the compact derives from THIS test's file bytes.
    """
    bridge_state_path = staging_dir.parent / "bridge-state.json"
    bridge_state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(bridge_state_path))
    gate._BRIDGE_STATE_SNAPSHOT = None
    gate._BRIDGE_STATE_SNAPSHOT_AFTER = None
    _populate_required_evidence(staging_dir)
    gate._generate_compact_evidence(staging_dir)


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
    for attr, layer_name in (
        ("layer1_reproducibility", "1_reproducibility"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
        ("layer6_full_e2e", "6_full_e2e"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name, **kwargs: gate.LayerResult(
                name=layer_name, passed=True
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
    for attr, layer_name in (
        ("layer2_replay", "2_replay"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
        ("layer6_full_e2e", "6_full_e2e"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name, **kwargs: gate.LayerResult(
                name=layer_name, passed=True
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


def _frozen_v4_fixture_pair_id(departure: str, return_date: str) -> str:
    """Build a well-formed canonical frozen-scenario ``date-pair:`` id via the
    canonical digest function, so the fixture always matches what the real
    producer seals for the frozen maldives scenario (C-122 supervision 01:10
    Block 1).  The three dates mirror a real run's effective-window seal (run
    date 2026-08-04 + 7-day lead): 08-11→08-16, 08-21→08-26, 08-31→09-07.
    """
    digest = frozen_v4_pair_id_digest(
        date.fromisoformat(departure), date.fromisoformat(return_date)
    )
    return f"date-pair:{departure}:{return_date}:{digest}"


# The passing fixture's pair-id set: the run's own checkpoint-bound sealed set.
# Ordered deterministically so per_pair indexes line up with ``pair_ids``.
_FIXTURE_PAIR_IDS = (
    _frozen_v4_fixture_pair_id("2026-08-11", "2026-08-16"),
    _frozen_v4_fixture_pair_id("2026-08-21", "2026-08-26"),
    _frozen_v4_fixture_pair_id("2026-08-31", "2026-09-07"),
)

# The shared fixture request identity every checkpoint binding must carry (the
# run's request SHA — C-122 supervision 18:13 wrong-request counter-example).
_FIXTURE_REQUEST_SHA256 = "f" * 64
# The per-pair dates mirror ``_FIXTURE_PAIR_IDS`` (in the same order), so each
# binding's dates agree with its pair id's own embedded dates.
_FIXTURE_PAIR_DATES = (
    ("2026-08-11", "2026-08-16"),
    ("2026-08-21", "2026-08-26"),
    ("2026-08-31", "2026-09-07"),
)


def _fixture_checkpoint_binding() -> dict[str, object]:
    """A self-consistent desensitized checkpoint binding for the passing
    fixture: three bindings (one per frozen pair), each carrying the checkpoint
    model's authoritative recomputable ``checkpoint_sha256`` over its OWN carried
    fields, an ordered digest chain with a recomputable chain digest, and ONE
    request identity shared by every binding (C-122 supervision 18:13 Fix 4).
    C-122 round-19 (gap 4): each binding is a ``completed`` checkpoint whose
    per-group query-task set is EXACTLY the canonical frozen graph's browser
    Source-id set, and whose ``run_summary_sha256`` recomputes from the carried
    business-summary fields via the checkpoint model's authoritative ``_run_summary``
    digest.  The validator recomputes every digest from these carried fields, so
    the fixture must be genuinely self-consistent — a copied / doctored digest
    fails the same-raw counter-example."""
    from tripchord.agents.live_jobs import LivePlanningPairCheckpoint

    bindings: list[dict[str, object]] = []
    for index, pair_id in enumerate(_FIXTURE_PAIR_IDS):
        departure_s, return_s = _FIXTURE_PAIR_DATES[index]
        query_task_ids = sorted(gate._V4_FROZEN_BROWSER_SOURCE_IDS)
        captured_at = "2026-08-10T00:00:00+00:00"
        # C-122 round-19 (gap 4): the full business-summary fields for a
        # COMPLETED checkpoint — run_purpose / finalization / decision typed, a
        # source-task count equal to the frozen per-pair query count, both
        # completion flags true, and NO failure class.
        run_summary_fields = {
            "state": "completed",
            "run_purpose": "exploration_and_publication",
            "finalization_state": "finalized",
            "decision_state": "recommended",
            "source_task_count": len(query_task_ids),
            "exploration_seal_passed": True,
            "all_platforms_complete": True,
            "failure_class": None,
        }
        run_summary_sha256 = LivePlanningPairCheckpoint._digest(
            LivePlanningPairCheckpoint._run_summary(run_summary_fields)
        )
        summary = LivePlanningPairCheckpoint._checkpoint_summary(
            {
                "schema_version": "live-pair-checkpoint-v1",
                "request_sha256": _FIXTURE_REQUEST_SHA256,
                "sequence": index + 1,
                "date_pair_id": pair_id,
                "departure_date": departure_s,
                "return_date": return_s,
                "state": "completed",
                "query_task_ids": query_task_ids,
                "run_summary_sha256": run_summary_sha256,
                "captured_at": captured_at,
            }
        )
        checkpoint_sha256 = LivePlanningPairCheckpoint._digest(summary)
        bindings.append(
            {
                "sequence": index + 1,
                "date_pair_id": pair_id,
                "departure_date": departure_s,
                "return_date": return_s,
                "state": "completed",
                "query_task_ids": query_task_ids,
                "query_task_ids_sha256": gate._canonical_sha256(query_task_ids),
                "run_purpose": run_summary_fields["run_purpose"],
                "finalization_state": run_summary_fields["finalization_state"],
                "decision_state": run_summary_fields["decision_state"],
                "source_task_count": run_summary_fields["source_task_count"],
                "exploration_seal_passed": run_summary_fields["exploration_seal_passed"],
                "all_platforms_complete": run_summary_fields["all_platforms_complete"],
                "failure_class": run_summary_fields["failure_class"],
                "run_summary_sha256": run_summary_sha256,
                "captured_at": captured_at,
                "checkpoint_sha256": checkpoint_sha256,
                "request_sha256": _FIXTURE_REQUEST_SHA256,
            }
        )
    ordered = [str(b["checkpoint_sha256"]) for b in bindings]
    return {
        "passed": True,
        "count": len(bindings),
        "ordered_checkpoint_sha256": ordered,
        "checkpoint_chain_sha256": gate._canonical_sha256(ordered),
        "request_sha256": _FIXTURE_REQUEST_SHA256,
        "bindings": bindings,
    }


def _per_check_evidence(name: str) -> dict[str, object]:
    """The structured, recomputable binding evidence each passing layer-6 check
    carries — field names match the live-v4 runner's evidence dicts (C-122
    Fix 3).  Values are synthetic and secret-free."""
    candidate_sha = "a" * 64
    return {
        "prefrozen_stay_plan_candidate_set": {
            "candidate_set_sha256": candidate_sha,
            "evidence_refs": [f"sha256:{candidate_sha}"],
        },
        "v4_source_graph": {
            # C-122 round-19 (supervision 17:03 Block 1): the fixture member sets
            # are DERIVED from the canonical frozen graph the gate script imports
            # (``_V4_FROZEN_*``), so the fixture always matches what the real
            # producer derives for the frozen maldives scenario.  The frozen
            # scenario schedules 13 browser queries per pair (6 enabled ctrip
            # kinds + 6 enabled qunar kinds + tongcheng's single flight) with 4
            # iCom Source tasks per pair.
            "expected_browser_tasks_per_pair": gate._V4_FROZEN_TASKS_PER_PAIR,
            "expected_browser_source_ids": sorted(
                gate._V4_FROZEN_BROWSER_SOURCE_IDS
            ),
            "expected_query_shapes": sorted(gate._V4_FROZEN_QUERY_SHAPES),
            "expected_icom_task_ids": sorted(gate._V4_FROZEN_ICOM_TASK_IDS),
            "pair_ids": list(_FIXTURE_PAIR_IDS),
            # C-122 supervision 01:10: the compact must carry the run's
            # checkpoint-bound sealed pair ids (an independent job-control-plane
            # record) so the validator can reject a foreign / swapped / missing /
            # extra pair set even when every id is well-formed.  For the passing
            # fixture the producer's pair_ids and the checkpoint record agree.
            "checkpoint_bound_pair_ids": list(_FIXTURE_PAIR_IDS),
            # C-122 supervision 18:13 (Fix 4): the compact must also carry the
            # full desensitized checkpoint binding — chain / dates / request /
            # content — so the validator independently re-verifies chain
            # integrity, the canonical date window and the request identity.
            "checkpoint_binding": _fixture_checkpoint_binding(),
            "total_planned_task_count": gate._V4_FROZEN_TASKS_PER_PAIR * 3,
            # C-122 HG-G: the frozen-scenario per-pair breakdown — 3 pairs x 13
            # browser/query tasks + 4 iCom tasks each, with the declared total
            # recomputable as the per-pair query-task sum.  Block 1 adds the exact
            # per-pair member lists so the validator can compare member sets.
            "per_pair": [
                {
                    "pair_id": _FIXTURE_PAIR_IDS[0],
                    "browser_source_task_ids": sorted(
                        gate._V4_FROZEN_BROWSER_SOURCE_IDS
                    ),
                    "query_task_ids": sorted(gate._V4_FROZEN_QUERY_SHAPES),
                    "icom_source_task_ids": sorted(gate._V4_FROZEN_ICOM_TASK_IDS),
                    "browser_source_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "query_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "icom_source_task_count": len(gate._V4_FROZEN_ICOM_TASK_IDS),
                },
                {
                    "pair_id": _FIXTURE_PAIR_IDS[1],
                    "browser_source_task_ids": sorted(
                        gate._V4_FROZEN_BROWSER_SOURCE_IDS
                    ),
                    "query_task_ids": sorted(gate._V4_FROZEN_QUERY_SHAPES),
                    "icom_source_task_ids": sorted(gate._V4_FROZEN_ICOM_TASK_IDS),
                    "browser_source_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "query_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "icom_source_task_count": len(gate._V4_FROZEN_ICOM_TASK_IDS),
                },
                {
                    "pair_id": _FIXTURE_PAIR_IDS[2],
                    "browser_source_task_ids": sorted(
                        gate._V4_FROZEN_BROWSER_SOURCE_IDS
                    ),
                    "query_task_ids": sorted(gate._V4_FROZEN_QUERY_SHAPES),
                    "icom_source_task_ids": sorted(gate._V4_FROZEN_ICOM_TASK_IDS),
                    "browser_source_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "query_task_count": gate._V4_FROZEN_TASKS_PER_PAIR,
                    "icom_source_task_count": len(gate._V4_FROZEN_ICOM_TASK_IDS),
                },
            ],
        },
        "stage_aware_exploration_publication_contract": {
            "exploration_count": 3,
            "publication_count": 2,
            "publication_option_ids": ["opt-1", "opt-2"],
        },
        "stay_inventory_four_state_contract": {
            "minimum_exact_providers_per_selected_segment": 2,
            "inventory_states": [
                "exact_quote",
                "confirmed_empty",
                "bounded_no_exact_quote",
                "bounded_provider_pending",
            ],
        },
        "planner_verifier_repair_master_stay_plan_chain": {
            "evidence_refs": ["stay-plan:plan-1"],
        },
        "recommendable_date_pair_stay_plan_options": {
            "freshness_ttl_seconds": 600,
            "freshness_by_option": {
                "opt-1": [
                    {
                        "component_id": "stay-1",
                        "captured_at": "2026-08-10T00:00:00+00:00",
                        "expires_at": "2026-08-10T00:10:00+00:00",
                        "age_seconds_at_post_event_gate": 5,
                        "ttl_seconds": 600,
                        "fresh_at_post_event_gate": True,
                    }
                ],
                "opt-2": [
                    {
                        "component_id": "stay-2",
                        "captured_at": "2026-08-10T00:00:00+00:00",
                        "expires_at": "2026-08-10T00:10:00+00:00",
                        "age_seconds_at_post_event_gate": 5,
                        "ttl_seconds": 600,
                        "fresh_at_post_event_gate": True,
                    }
                ],
            },
        },
        "icom_exploration_and_publication_evidence": {
            "publication_target_task_ids": ["public-transfer-icom-ctrip-1"],
            "exploration_full_coverage": {"passed": True},
        },
        "all_recommended_publication_closures": {
            "options": {
                "opt-1": {
                    "evidence_scope": "publication",
                    "planner_verifier_repair": {"passed": True},
                    "budget_and_selected_evidence": {"passed": True},
                    "public_transfer_evidence": {"passed": True},
                }
            }
        },
        "real_v4_browser_source_evidence": {
            "source_task_count": 5,
            "snapshot_count": 5,
            "successful_snapshot_count": 5,
            "bounded_or_empty_task_count": 0,
        },
        "flight_search_outcome_contract": {
            "provider_outcome_states": {
                "ctrip": "quote_found",
                "qunar": "comparison_price_only",
                "tongcheng": "bounded_no_exact_quote",
            },
            "exact_provider_count": 1,
            "comparison_provider_count": 1,
            "price_bearing_provider_count": 2,
        },
        "observed_cross_platform_overlap": {
            "interval_count": 3,
            "max_overlapping_tasks": 3,
            "max_overlapping_providers": 3,
        },
        "strict_selected_plan_platform_coverage": {
            "selected_stay_plan_id": "plan-1",
            "providers": ["ctrip", "qunar", "tongcheng"],
            "coverage_mode": "strict",
            "all_platforms_complete": True,
        },
        "planner_verifier_repair_orchestrator": {
            "graph_chain_ok": True,
            "reverify_node_present": True,
            "recritic_stage_completed": True,
            "planning_handoff_present": True,
            "stage_handoffs_match": True,
            "identity_chain_ok": True,
            "reason_chain_ok": True,
            "independent_audit_present": True,
            "independent_audit_passed": True,
            "independent_check_count": 5,
            "planner_candidate_id": "cand-1",
            "initial_verifier_candidate_id": "cand-1",
            "repaired_candidate_id": None,
            "reverified_candidate_id": "cand-1",
            "decision_states": ["accept"],
            "repair_execution_mode": "verified_noop",
        },
        "exact_budget_and_selected_evidence": {
            "computed_total_cents": 1000,
            "declared_total_cents": 1000,
            "evidence_ref_count": 5,
            "selected_icom_transfer_count": 0,
            "supplemental_usd_cents": None,
            "published_base_fare_boundary_ok": True,
        },
        "event_injection_repair_reverify_master": {
            "dynamic_replan": {"passed": True},
            "read_only_graph": {"passed": True},
            "initial_stay_plan_id": "plan-1",
            "event_final_stay_plan_id": "plan-1",
        },
    }[name]


def _matching_done_gate() -> dict[str, object]:
    """A passing layer-6 ``done_gate`` report in the real runner schema:
    ``passed`` plus the full 15-item check set, each item passed (the actual
    ``LiveV4DoneGateReport`` shape — there is no top-level ``passed``).  Each
    item carries the structured per-check evidence its semantic group verifies
    (C-122 Fix 3)."""
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
    checks = []
    for name in check_names:
        evidence = _per_check_evidence(name)
        refs = evidence.get("evidence_refs")
        checks.append(
            {
                "name": name,
                "passed": True,
                "summary": "ok",
                "evidence_refs": list(refs) if isinstance(refs, list) else [],
                "evidence": evidence,
            }
        )
    return {"passed": True, "checks": checks}


def _per_scope_canary_evidence(scope: str) -> dict[str, object]:
    """The desensitized per-scope evidence binding a passing canary carries:
    companion-heartbeat identity for browser scopes, the read-only query sample
    for icom (C-122 Fix 3).  Values are synthetic and secret-free."""
    if scope == "icom:transfer":
        return {
            "searched_at": "2026-08-10T00:00:00+00:00",
            "options": 3,
            "sample": {
                "service_name": "speed-boat",
                "departure_at": "2026-08-13T09:00:00+00:00",
                "fare_amount": "150",
                "currency": "USD",
            },
            "source_url_count": 3,
        }
    return {
        "companion_id": "comp-1",
        "providers": ["ctrip", "qunar", "tongcheng"],
        "authorized_scope_keys": [scope],
        "is_fresh": True,
        "age_seconds": 3,
        "adapter_version": "test-adapter",
        "contract_version": "tripchord-browser-bridge-v2",
        # C-122 round-18 gate-1: the heartbeat receipt also names the runtime
        # instance that performed the handshake.
        "runtime_instance_id": "runtime-1",
    }


def _matching_canary() -> dict[str, object]:
    """A passing certified-OTA canary in the real schema: top-level ``passed``
    plus the complete certified canary scope set derived from the authoritative
    registry — the five certified browser Companion OTA scopes (ctrip:flight,
    ctrip:lodging, qunar:flight, qunar:lodging, tongcheng:flight) plus the iCom
    public-API scope, 6 total — each fresh/authorized/read_only/passed.  The
    connected Companion authorizes EXACTLY the five certified browser scopes;
    ``icom:transfer`` is a public-API scope and never appears in
    ``authorized_scope_keys``, and the DISABLED ``tongcheng:lodging`` never
    enters the canary (C-122 round-19 17:03 veto)."""
    browser_scopes = tuple(sorted(gate._CERTIFIED_OTA_SCOPES))
    scopes = (
        *((scope, "companion_heartbeat") for scope in browser_scopes),
        ("icom:transfer", "icom_public_api"),
    )
    return {
        "passed": True,
        "bridge_token_present": True,
        "scopes": [
            (
                {
                    "scope": scope,
                    "kind": kind,
                    # C-122 round-18 gate-1: the certified canary carries the real
                    # provider of each scope (the scope's platform prefix).
                    "provider": scope.split(":", 1)[0],
                    "passed": True,
                    "fresh": True,
                    "authorized": True,
                    "read_only": True,
                    # C-122 Fix 3: each scope carries its desensitized per-scope
                    # evidence binding (companion heartbeat / read-only query
                    # sample) in the raw canary, which the compact preserves.
                    "evidence": _per_scope_canary_evidence(scope),
                }
            )
            for scope, kind in scopes
        ],
        "companion_status": {
            "status": "connected",
            "stale_after_seconds": 45,
            "companions": [
                {
                    "companion_id": "comp-1",
                    "providers": ["ctrip", "qunar", "tongcheng"],
                    # C-122 round-19: exactly the five certified browser scopes
                    # — no ``icom:transfer``, no DISABLED ``tongcheng:lodging``.
                    "authorized_scope_keys": list(browser_scopes),
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


def _seal_canary_failure_diagnostic(
    output_path: Path,
    *,
    run_id: str,
    tested_sha: str,
    stage: str = "evaluate",
    message: str = "upstream provider 401",
) -> Path:
    """Write a REAL 0600 canary failure diagnostic using the canary's OWN sealing
    code (``_seal_failure_diagnostic``) — the exact function the canary's main()
    calls on a crash.  The message embeds a token-shaped secret (40 chars) so the
    tests prove the diagnostic and its consumption are desensitized."""
    from benchmarks import live_canary_certified as canary

    return canary._seal_failure_diagnostic(
        stage,
        RuntimeError(f"{message} {'S' * 40}"),
        output_path,
        run_id=run_id,
        tested_sha=tested_sha,
    )


def test_layer5_consumes_canary_failure_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (supervision 17:03 Block 2 counter-example): a crashed
    canary (non-zero exit + no main JSON) writes a 0600 failure diagnostic; the
    outer layer MUST read it, verify schema / run_id / tested_sha / runtime /
    perms / freshness, and keep the desensitized classification + bindings in the
    layer-5 detail — never discard the failure trail."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "layer-5 must keep the canary failure classification+binding"
    detail = diag_checks[0]["detail"]
    assert "stage=evaluate" in detail
    assert "exception=RuntimeError" in detail
    assert f"run_id={run_id}" in detail
    assert f"tested_sha={tested_sha[:12]}" in detail
    assert "runtime=python" in detail
    # The token-shaped secret in the crash message is redacted, never echoed.
    assert "S" * 40 not in detail
    assert "[REDACTED]" in detail


def test_layer5_consumes_real_subprocess_sealed_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (Block 2 counter-example, REAL subprocess): the canary's
    diagnostic is sealed by an actual subprocess execution of its own sealing
    code; the outer layer then reads it, verifies the 0600 perms + schema +
    run_id + tested_sha + runtime, and keeps the desensitized classification in
    the layer-5 detail."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    sealed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from benchmarks.live_canary_certified import _seal_failure_diagnostic; "
            "_seal_failure_diagnostic('evaluate', RuntimeError('upstream provider 401'), "
            "Path(sys.argv[1]), run_id=sys.argv[2], tested_sha=sys.argv[3])",
            str(evidence_path),
            run_id,
            tested_sha,
        ],
        capture_output=True,
        text=True,
        cwd=gate.ROOT,
    )
    assert sealed.returncode == 0, sealed.stderr
    diag_path = evidence_path.with_suffix(evidence_path.suffix + ".failure.json")
    assert diag_path.is_file()
    assert stat.S_IMODE(diag_path.stat().st_mode) == 0o600
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "layer-5 must keep the subprocess-sealed classification+binding"
    detail = diag_checks[0]["detail"]
    assert "stage=evaluate" in detail
    assert "exception=RuntimeError" in detail
    assert f"run_id={run_id}" in detail
    assert f"tested_sha={tested_sha[:12]}" in detail
    assert "runtime=python" in detail


def test_layer5_rejects_mismatched_canary_failure_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (Block 2 counter-example): a failure diagnostic that binds a
    DIFFERENT run_id than this run must fail closed explicitly — a foreign or old
    diagnostic is never silently consumed as evidence of THIS failure."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    # Diagnostic from a PRIOR / different run: different run_id and tested_sha.
    _seal_canary_failure_diagnostic(
        evidence_path, run_id="000000000000", tested_sha="b" * 40
    )
    result = gate.layer5_real_canary(
        staging_dir, run_id="abc123def456", tested_commit_sha="a" * 40
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "canary failure diagnostic run_id" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_foreign_canary_failure_runtime_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 3 counter-example (fake runtime): a failure
    diagnostic whose runtime identity is merely NON-EMPTY (``EVIL-RUNTIME`` /
    ``EVIL-PLATFORM``) must fail closed — the diagnosis runtime must be bound
    EXACTLY to the gate's own authoritative interpreter identity (the canary runs
    as a child of this process under the same interpreter), never accepted by
    being present."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["run_identity"]["runtime"] = {
        "python": "EVIL-RUNTIME",
        "platform": "EVIL-PLATFORM",
    }
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "runtime identity" in joined and "authoritative runtime" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_wrong_canary_failure_python_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 3 counter-example (wrong python): a failure
    diagnostic whose runtime.python is a plausible-looking but DIFFERENT version
    (``1.2.3``) must fail closed — the runtime binding is an exact match against
    the authoritative interpreter, not a format check."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["run_identity"]["runtime"]["python"] = "1.2.3"
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "runtime identity" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_sanitizes_credential_bearing_canary_failure_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 3 counter-example (credential-bearing
    summary): a failure summary carrying a URL + a token-shaped secret + a phone
    number must be RE-SANITIZED by the consumer before it lands in the layer-5
    detail — the raw credential never reaches the committed trail even when the
    producer's own desensitization was bypassed."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["summary"] = (
        "upstream refused https://evil.example/token "
        "abcdef0123456789abcdef0123456789abcdef01 for account 13812345678"
    )
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "a valid (this-run, fresh) diagnostic still keeps its classification"
    detail = diag_checks[0]["detail"]
    assert "https://evil.example/token" not in detail
    assert "abcdef0123456789abcdef0123456789abcdef01" not in detail
    assert "13812345678" not in detail
    assert "<url>" in detail and "[REDACTED]" in detail


def test_layer5_redacts_short_and_structured_secret_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (dotted token / AKIA-like / short
    opaque token=): a failure summary carrying an AKIA-style AWS access key
    (20 chars), a dotted bearer token whose first two segments are each under the
    32-char run threshold, and a SHORT ``token=value`` assignment must ALL be
    collapsed to ``<redacted>`` by the consumer — none may reach the committed
    layer-5 detail as-is."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    akia_key = "AKIAIOSFODNN7EXAMPLE"
    dotted = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    short_secret = "token=xJ3kQm9pR2sW"
    diagnostic["summary"] = (
        f"upstream refused aws_key={akia_key} jwt={dotted} {short_secret} "
        "for booking 9988776655"
    )
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "a valid (this-run, fresh) diagnostic still keeps its classification"
    detail = diag_checks[0]["detail"]
    assert akia_key not in detail
    assert dotted not in detail
    assert "xJ3kQm9pR2sW" not in detail
    assert "[REDACTED]" in detail


def test_layer5_redacts_shortest_and_prefixed_secret_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 02:56 (Block 2) counter-example: the consumer's
    sanitizer must ALSO collapse the shapes the 32+ run / 6-char KV floor /
    6-char dotted floor miss — a 3-char ``token=abc`` assignment, a GitHub
    ``ghp_`` prefixed token, a short Bearer form and a dotted token whose last
    segment is only 3 chars.  None may reach the committed layer-5 detail."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    ghp_token = "ghp_abcdef1234567890"
    short_bearer = "Bearer abcd1234"
    short_jwt = "eyJh.eyJzd.abc"
    short_kv = "token=abc"
    diagnostic["summary"] = (
        f"upstream refused {ghp_token} {short_bearer} {short_jwt} {short_kv} "
        "for booking 9988776655"
    )
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "a valid (this-run, fresh) diagnostic still keeps its classification"
    detail = diag_checks[0]["detail"]
    assert ghp_token not in detail
    assert "abcd1234" not in detail
    assert short_jwt not in detail
    assert short_kv not in detail
    assert "[REDACTED]" in detail


def test_canary_producer_seal_desensitizes_short_credential_shapes(
    tmp_path: Path,
) -> None:
    """C-122 supervision 02:56 (Block 2) counter-example: the PRODUCER's own
    ``_seal_failure_diagnostic`` must sanitize short / structured credential
    shapes in the artifact it writes to disk — an AKIA key, a GitHub ``ghp_``
    token, a short ``token=abc``, a short dotted JWT, a short Bearer form and a
    credential-bearing URL must NEVER appear raw in the sealed ``<output>
    .failure.json`` ``summary`` field (previously only 32+ runs were masked)."""
    from benchmarks import live_canary_certified as canary

    output = tmp_path / "live-canary-certified.json"
    akia_key = "AKIAIOSFODNN7EXAMPLE"
    ghp_token = "ghp_abcdef1234567890"
    short_bearer = "Bearer abcd1234"
    short_jwt = "eyJh.eyJzd.abc"
    short_kv = "token=abc"
    url = "https://evil.example/oauth2/callback?code=shortsecret123"
    message = (
        f"upstream 401 aws_key={akia_key} gh={ghp_token} {short_bearer} "
        f"jwt={short_jwt} {short_kv} {url}"
    )
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(message),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    summary = diagnostic["summary"]
    assert akia_key not in summary
    assert "abcdef1234567890" not in summary
    assert "abcd1234" not in summary
    assert short_jwt not in summary
    assert short_kv not in summary
    assert "evil.example" not in summary
    assert "[REDACTED]" in summary


def test_canary_producer_desensitize_catches_short_credential_shapes() -> None:
    """C-122 supervision 02:56 (Block 2): the producer's ``_desensitize`` — the
    sanitizer the canary's stderr crash paths and iCom replay error paths use —
    collapses every short / structured credential shape, so a failure message
    can never echo them on stderr either."""
    from benchmarks import live_canary_certified as canary

    for raw, forbidden in (
        ("aws_key=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("token ghp_abcdef1234567890", "ghp_abcdef1234567890"),
        ("credential github_pat_abcDEF123456_XY", "github_pat_abcDEF123456_XY"),
        ("auth Bearer abcd1234", "abcd1234"),
        ("jwt=eyJh.eyJzd.abc", "eyJh.eyJzd.abc"),
        ("password=abc", "password=abc"),
        ("https://evil.example/cb?token=shortsecret123", "evil.example"),
    ):
        out = canary._desensitize(raw)
        assert forbidden not in out, f"{forbidden!r} leaked in {out!r}"
        assert "[REDACTED]" in out or "<url>" in out


def test_canary_producer_desensitize_catches_whole_header_forms() -> None:
    """C-122 supervision 03:46 (Block 1) + 04:14 counter-example (stderr layer):
    the producer's ``_desensitize`` must mask whole header FIELDS name-and-value
    together — a ``Basic`` base64 body (the opaque-KV pattern stops at the space
    after ``Basic`` and would leave the base64 visible), a ``;``-joined cookie
    pair, an ``X-API-Key`` and ``Set-Cookie`` / ``Proxy-Authorization`` values
    must never survive on stderr with the credential body intact.  04:14: any
    non-empty value is masked whole — no ``{4,}`` character floor
    (``Cookie:a=b`` / ``X-API-Key:abc``) and a leading quote does not preserve
    the body (``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc;
    HttpOnly"`` / ``X-API-Key: "abc123"``)."""
    from benchmarks import live_canary_certified as canary

    for raw, forbidden in (
        (
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
        ),
        (
            "Cookie: sessionid=abc123; csrftoken=xyz789",
            "sessionid=abc123",
        ),
        (
            "X-API-Key: sk-live-abc123def456",
            "sk-live-abc123def456",
        ),
        (
            "Set-Cookie: session=def456; HttpOnly",
            "session=def456",
        ),
        (
            "Proxy-Authorization: Bearer abcd1234wxyz",
            "abcd1234wxyz",
        ),
        # C-122 supervision 04:14: short (3-char) values and quoted bodies.
        (
            "Cookie:a=b",
            "a=b",
        ),
        (
            "X-API-Key:abc",
            "abc",
        ),
        (
            'Authorization: "Basic YWJjZA=="',
            "YWJjZA==",
        ),
        (
            'Set-Cookie: "sid=abc; HttpOnly"',
            "sid=abc",
        ),
        (
            'X-API-Key: "abc123"',
            "abc123",
        ),
    ):
        out = canary._desensitize(raw)
        assert forbidden not in out, f"{forbidden!r} leaked in {out!r}"
        assert "[REDACTED]" in out


def test_canary_producer_seal_desensitizes_whole_header_forms(
    tmp_path: Path,
) -> None:
    """C-122 supervision 03:46 (Block 1) + 04:14 counter-example (raw failure
    JSON layer): the PRODUCER's own ``_seal_failure_diagnostic`` must mask whole
    Authorization / Cookie / X-API-Key header fields name-and-value together in
    the ``<output>.failure.json`` ``summary`` — a ``Basic`` base64 credential
    body, a cookie pair and an API-key header must NEVER appear raw in the
    committed diagnostic, including the 04:14 short (3-char) and quoted forms
    (``Cookie:a=b`` / ``X-API-Key:abc`` / ``Authorization: "Basic YWJjZA=="`` /
    ``Set-Cookie: "sid=abc; HttpOnly"`` / ``X-API-Key: "abc123"``)."""
    from benchmarks import live_canary_certified as canary

    output = tmp_path / "live-canary-certified.json"
    basic_body = "dXNlcjpwYXNzd29yZA=="
    cookie_body = "sessionid=abc123; csrftoken=xyz789"
    api_key = "sk-live-abc123def456"
    message = (
        f"upstream 401 Authorization: Basic {basic_body} "
        f"Cookie: {cookie_body} X-API-Key: {api_key} "
        'Cookie:a=b X-API-Key:abc Authorization: "Basic YWJjZA==" '
        'Set-Cookie: "sid=abc; HttpOnly" X-API-Key: "abc123"'
    )
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(message),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    summary = diagnostic["summary"]
    assert basic_body not in summary
    assert cookie_body not in summary
    assert api_key not in summary
    assert "YWJjZA==" not in summary
    assert "a=b" not in summary
    assert "abc123" not in summary
    assert "[REDACTED]" in summary


def test_layer5_redacts_whole_header_forms_from_final_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 03:46 (Block 1) + 04:14 counter-example (final report
    layer): the CONSUMER's sanitizer must mask whole Authorization / Cookie /
    X-API-Key header fields name-and-value together before the layer-5 detail
    lands in the committed report — a ``Basic`` base64 body and a ``;``-joined
    cookie pair must never reach the committed layer-5 detail as-is, and the
    04:14 short (3-char) / quoted forms (``Cookie:a=b`` / ``X-API-Key:abc`` /
    ``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc; HttpOnly"`` /
    ``X-API-Key: "abc123"``) must not either."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "x9y8z7w6v5u4"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    basic_body = "dXNlcjpwYXNzd29yZA=="
    cookie_body = "sessionid=abc123; csrftoken=xyz789"
    api_key = "sk-live-abc123def456"
    diagnostic["summary"] = (
        f"upstream refused Authorization: Basic {basic_body} "
        f"Cookie: {cookie_body} X-API-Key: {api_key} "
        'Cookie:a=b X-API-Key:abc Authorization: "Basic YWJjZA==" '
        'Set-Cookie: "sid=abc; HttpOnly" X-API-Key: "abc123"'
    )
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "a valid (this-run, fresh) diagnostic still keeps its classification"
    detail = diag_checks[0]["detail"]
    assert basic_body not in detail
    assert cookie_body not in detail
    assert api_key not in detail
    assert "YWJjZA==" not in detail
    assert "a=b" not in detail
    assert "abc123" not in detail
    assert "[REDACTED]" in detail


def test_secret_scan_rejects_whole_header_forms_in_failure_diagnostic(
    tmp_path: Path,
) -> None:
    """C-122 supervision 03:46 (Block 1) + 04:14 defense-in-depth: even if a
    producer bypass were to write a whole Authorization / Cookie / X-API-Key
    header into a free-form diagnostic, the staging secret scan must fail the
    gate closed before the file is certified — including the 04:14 short
    (3-char) / quoted forms (``Cookie:a=b`` / ``X-API-Key:abc`` /
    ``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc; HttpOnly"`` /
    ``X-API-Key: "abc123"``)."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    diag = staging_dir / "live-canary-certified.json.failure.json"
    diag.write_text(
        json.dumps(
            {
                "schema_version": "x",
                "summary": (
                    "Authorization: Basic dXNlcjpwYXNzd29yZA== "
                    "X-API-Key: sk-live-abc123def456 "
                    'Cookie:a=b X-API-Key:abc '
                    'Authorization: "Basic YWJjZA==" '
                    'Set-Cookie: "sid=abc; HttpOnly" X-API-Key: "abc123"'
                ),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="secret leak"):
        gate._secret_scan_staging(staging_dir, gate._SecretNeedles(()))


def test_secret_scan_rejects_short_credential_shapes_in_failure_diagnostic(
    tmp_path: Path,
) -> None:
    """C-122 supervision 02:56 (Block 2): defense-in-depth — even if a producer
    bypass were to write a short credential into a free-form diagnostic, the
    staging secret scan must fail the gate closed before the file is certified."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    diag = staging_dir / "live-canary-certified.json.failure.json"
    diag.write_text(
        json.dumps(
            {
                "schema_version": "x",
                "summary": "token=abc ghp_abcdef1234567890 AKIAIOSFODNN7EXAMPLE",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="secret leak"):
        gate._secret_scan_staging(staging_dir, gate._SecretNeedles(()))


def test_layer5_rejects_canary_diagnostic_with_unknown_top_level_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (未知字段): a failure diagnostic
    carrying an UNKNOWN top-level field must FAIL CLOSED — the diagnostic is a
    structured contract, so an extra smuggled field (credential dump, producer
    bypass) is rejected before its summary is consumed, even when every known
    field is valid and current."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["smuggled_payload"] = {"secret": "x" * 40}
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "unknown field(s)" in joined and "smuggled_payload" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_canary_diagnostic_with_unknown_run_identity_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (未知字段): a failure diagnostic
    whose ``run_identity`` carries an unknown field fails closed the same way — a
    structured whitelist applies to every nested object, not just the top level."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["run_identity"]["forged_claim"] = "EVIL-OWNER"
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "run_identity carries unknown field(s)" in joined
    assert "forged_claim" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_canary_diagnostic_with_unknown_runtime_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (未知字段): a failure diagnostic
    whose ``run_identity.runtime`` carries an unknown field fails closed — the
    runtime identity is an exact contract, never an open bag."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["run_identity"]["runtime"]["env_dump"] = "EVIL"
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "runtime carries unknown field(s)" in joined
    assert "env_dump" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_stale_canary_failure_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (Block 2 counter-example): a failure diagnostic that binds
    THIS run but is STALE (generated long ago) must fail closed explicitly — old
    failure evidence is never reused for a current run."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "abc123def456"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["generated_at"] = (
        datetime.now(UTC) - timedelta(hours=2)
    ).isoformat()
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "diagnostic is stale" in joined
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )


def test_layer5_rejects_missing_canary_failure_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (Block 2 counter-example): a crashed canary (non-zero exit,
    no main JSON) with NO failure diagnostic must fail closed explicitly — a
    missing diagnostic cannot be papered over."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    result = gate.layer5_real_canary(
        staging_dir, run_id="abc123def456", tested_commit_sha="a" * 40
    )
    assert result.passed is False
    joined = " ; ".join(str(c.get("detail", "")) for c in result.sub_checks)
    assert "no failure diagnostic" in joined


def test_layer5_recovery_success_does_not_reuse_old_failure_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-19 (Block 2 counter-example): a recovery-replay SUCCESS (exit 0
    + valid passed=true report) must NOT reuse an old failure diagnostic sitting at
    the output path — fresh success clears/replaces stale failure evidence, and the
    layer only consumes a diagnostic when the CURRENT run failed."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    evidence_path = staging_dir / "live-canary-certified.json"
    # A PRIOR failed run left a stale diagnostic behind.
    _seal_canary_failure_diagnostic(
        evidence_path, run_id="000000000000", tested_sha="b" * 40
    )
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(_matching_canary()), encoding="utf-8"
    )
    result = gate.layer5_real_canary(
        staging_dir, run_id="abc123def456", tested_commit_sha="a" * 40
    )
    assert result.passed is True
    assert not any(
        c.get("name") == "canary_failure_diagnostic" for c in result.sub_checks
    )
    assert not any(
        "failure diagnostic" in str(c.get("detail", "")) for c in result.sub_checks
    )


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
    """C-122 P0 side-channel publish: the evidence commits E (parent=S) and P
    (parent=E) are built entirely off the shared worktree — the product branch /
    HEAD / real index / worktree stay byte-for-byte at S and only the dedicated
    gate ref ``refs/tripchord/done-gate/<run_id>`` appears, atomically, at the
    very end.  E/P are never installed as the branch tip."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)

    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The branch never moved and only the gate ref was created: HEAD/index/
    # worktree at S, P^=E, E^=S, evidence commits unreachable from the branch.
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )

    # P's committed tree carries the report + manifest + evidence files.
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", pointer_sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in (
        "benchmarks/results/product-acceptance.json",
        "benchmarks/results/browser-e2e.json",
        "benchmarks/results/browser-e2e-screenshot.png",
        gate._REPORT_REL,
        gate._MANIFEST_REL,
    ):
        assert rel in tree, f"missing committed evidence path in P: {rel}"

    # The authoritative report in P records tested_commit_sha=S and
    # evidence_commit=E, distinct, and binds the gate ref.
    committed = json.loads(
        subprocess.run(
            ["git", "-C", str(clean_repo), "show", f"{pointer_sha}:{gate._REPORT_REL}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert committed["tested_commit_sha"] == tested_sha
    assert committed["evidence_commit"] == evidence_commit
    assert committed["evidence_commit"] != tested_sha
    assert committed["passed"] is True
    assert committed["gate_ref"] == f"refs/tripchord/done-gate/{_TEST_RUN_ID}"

    # The staged (delivered) report in the exclusive staging dir is the same
    # file P committed — never a tracked-worktree copy.
    staged = json.loads(
        (staging_dir / gate._REPORT_STAGED_NAME).read_text(encoding="utf-8")
    )
    assert staged["evidence_commit"] == evidence_commit
    assert staged["passed"] is True


# ---------------------------------------------------------------------------
# C-122 round-18 gate-7: formal resolver / verification entry
# (verify P->E->S and every committed blob from refs/tripchord/done-gate/<run_id>)
# ---------------------------------------------------------------------------


def test_verify_gate_ref_verifies_published_chain(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-7 positive: after a real side-channel publish,
    ``verify_gate_ref`` resolves ``refs/tripchord/done-gate/<run_id>`` and
    verifies the full P->E->S chain plus every committed evidence blob against
    the committed manifests."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is True
    assert verdict["pointer_commit"] == pointer_sha
    assert verdict["evidence_commit"] == evidence_commit
    assert verdict["tested_commit_sha"] == tested_sha
    assert verdict["gate_ref"] == f"refs/tripchord/done-gate/{_TEST_RUN_ID}"


def test_verify_gate_ref_fails_closed_on_unpublished_run_id(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 round-18 gate-7 counter-example: a run_id with no published gate ref
    is a verified=False verdict naming the missing ref — never a pass."""
    _patch_root(monkeypatch, clean_repo)
    verdict = gate.verify_gate_ref("ffffffffffff")
    assert verdict["verified"] is False
    assert any("does not exist" in problem for problem in verdict["problems"])


def test_verify_gate_ref_detects_ref_repointed_off_pointer(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-7 counter-example: if the gate ref is repointed away
    from the pointer commit P (here, onto E), the chain P->E->S no longer holds
    and the resolver fails closed instead of trusting the ref."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    # Repoint the ref at E: the resolver must fail closed (E's committed report
    # carries no evidence_commit binding, and the chain from E is no longer P).
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            evidence_commit,
            pointer_sha,
        ],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert verdict["problems"]


def test_verify_gate_ref_latest_resolves_single_ref(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-7: ``--latest`` resolution finds the only published
    ref under the namespace."""
    report, start, _tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    gate._commit_evidence(staging_dir, report, start=start)
    assert gate._latest_gate_run_id() == _TEST_RUN_ID


def test_main_verify_ref_mode_prints_verdict(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, capsys
) -> None:
    """C-122 round-18 gate-7: the ``--verify-ref`` CLI mode prints a single JSON
    verdict and returns 0 for a verified trail, 2 for an unknown run_id — and it
    never touches the worktree (no staging dir, no layers)."""
    report, start, _tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    gate._commit_evidence(staging_dir, report, start=start)
    rc = gate.main(["--verify-ref", _TEST_RUN_ID])
    captured = capsys.readouterr()
    verdict = json.loads(captured.out)
    assert rc == 0
    assert verdict["verified"] is True
    # An unknown run_id is exit 2 with a fail-closed verdict.
    rc = gate.main(["--verify-ref", "ffffffffffff"])
    captured = capsys.readouterr()
    verdict = json.loads(captured.out)
    assert rc == 2
    assert verdict["verified"] is False


def test_main_verify_ref_latest_is_composable(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, capsys
) -> None:
    """C-122 HG-D counter-example: ``--verify-ref --latest`` must be composable —
    argparse must NOT exit 2 demanding a RUN_ID before the latest-resolver runs;
    the CLI resolves the most recently published ref and verifies it."""
    report, start, _tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    gate._commit_evidence(staging_dir, report, start=start)
    rc = gate.main(["--verify-ref", "--latest"])
    captured = capsys.readouterr()
    verdict = json.loads(captured.out)
    assert rc == 0
    assert verdict["verified"] is True
    assert verdict.get("run_id") == _TEST_RUN_ID


def test_main_verify_ref_without_run_id_fails_closed(capsys) -> None:
    """C-122 HG-D counter-example: ``--verify-ref`` with neither a RUN_ID nor
    ``--latest`` is a parameter mistake and fails closed with a JSON problem
    payload (not argparse's bare exit-2 usage error)."""
    rc = gate.main(["--verify-ref"])
    captured = capsys.readouterr()
    assert rc == 2
    verdict = json.loads(captured.out)
    assert "RUN_ID or --latest" in verdict["problems"][0]


def test_main_latest_without_verify_ref_fails_closed(capsys) -> None:
    """C-122 round-18 gate-3 counter-example: a bare ``--latest`` is a parameter
    mistake — it can only pick which published run to verify — and must fail
    closed with a JSON problem payload and exit 2 instead of silently ignoring
    the flag."""
    rc = gate.main(["--latest"])
    captured = capsys.readouterr()
    assert rc == 2
    verdict = json.loads(captured.out)
    assert "--latest requires --verify-ref" in verdict["problems"]


def test_commit_evidence_rejects_foreign_layer_name(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-3 counter-example: the publishing side re-parses P's
    authoritative report from the committed blob and requires the layer set to be
    EXACTLY the six fixed names — a renamed/replaced/foreign layer is not a
    done-gate pass even when every layer says passed=true/skipped=false, and
    nothing is published."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    report.layers = [
        gate.LayerResult(name="7_extra", passed=True, skipped=False),
        gate.LayerResult(name="2_replay", passed=True, skipped=False),
        gate.LayerResult(name="3_clean_chrome_fixtures", passed=True, skipped=False),
        gate.LayerResult(name="4_model_smoke", passed=True, skipped=False),
        gate.LayerResult(name="5_real_canary", passed=True, skipped=False),
        gate.LayerResult(name="6_full_e2e", passed=True, skipped=False),
    ]
    with pytest.raises(gate.GateStateChangedError, match="six fixed layer names"):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, tested_sha)


def test_commit_evidence_rejects_skipped_layer_in_pointer_report(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-3 counter-example: a layer recorded as skipped=true in
    the pointer report fails closed on the publishing side — a certified pass
    must bind every layer with skipped=false, not merely list six layers."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    report.layers = [
        gate.LayerResult(name="1_reproducibility", passed=True, skipped=False),
        gate.LayerResult(name="2_replay", passed=True, skipped=False),
        gate.LayerResult(name="3_clean_chrome_fixtures", passed=True, skipped=False),
        gate.LayerResult(name="4_model_smoke", passed=True, skipped=False),
        gate.LayerResult(name="5_real_canary", passed=True, skipped=False),
        gate.LayerResult(name="6_full_e2e", passed=True, skipped=True),
    ]
    with pytest.raises(gate.GateStateChangedError, match="is not skipped=false"):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, tested_sha)


def test_verify_gate_ref_fails_closed_on_foreign_layer_set(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    tmp_path: Path,
) -> None:
    """C-122 round-18 gate-3 counter-example: the consumer resolver re-reads P's
    authoritative report from the committed blob and requires the layer set to be
    EXACTLY the six fixed names — a manually-forged pointer whose report renames
    one layer is verified=False, never a pass."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    # Rebuild P's tree with a tampered report (one layer renamed) and repoint the
    # gate ref at the forged pointer; the chain P2->E->S still holds.
    report_blob = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "cat-file",
            "blob",
            f"{p_sha}:{gate._REPORT_REL}",
        ],
        capture_output=True,
        check=True,
    ).stdout
    data = json.loads(report_blob.decode("utf-8"))
    data["layers"][0]["name"] = "7_foreign"
    forged_blob = subprocess.run(
        ["git", "-C", str(clean_repo), "hash-object", "-w", "--stdin"],
        input=json.dumps(data).encode("utf-8"),
        capture_output=True,
        check=True,
    ).stdout.strip()
    index = tmp_path / "idx"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(["git", "-C", str(clean_repo), "read-tree", p_sha], env=env, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{forged_blob.decode()},{gate._REPORT_REL}",
        ],
        env=env,
        check=True,
    )
    tree2 = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    p2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            tree2,
            "-p",
            evidence_commit,
            "-m",
            "tampered pointer (foreign layer)",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            p2,
            p_sha,
        ],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "six fixed layer names" in problem for problem in verdict["problems"]
    )


def _cat_blob(repo: Path, spec: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", spec],
        capture_output=True,
        check=True,
    ).stdout


def _hash_blob(repo: Path, data: bytes) -> str:
    # ``hash-object --stdin`` consumes the EXACT bytes — never run in text mode
    # where subprocess would try to re-encode the bytes through the locale.
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=data,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


def _forge_repointed_chain(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    tmp_path: Path,
    *,
    mutate_e_manifest: Callable[[dict[str, object]], dict[str, object]],
    drop_from_e: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Publish a real trail, then forge a CONSISTENT E2/P2 chain (S -> E2 -> P2)
    and repoint the gate ref at P2.

    ``mutate_e_manifest`` receives the parsed E manifest and returns the forged
    manifest dict (which P2's manifest also derives from, bound to E2);
    ``drop_from_e`` names repo-relative paths to remove from E2's tree.  The
    forgery is full-graph: E2 = S + (E tree minus drops, forged manifest), P2 =
    E2 + (report/manifest bound to E2), so the consumer resolver sees a coherent
    chain whose violation is exactly what each counter-example targets.  Returns
    (forged_e2, forged_p2).
    """
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    index = tmp_path / "forge-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    # E2 tree = E's tree minus dropped paths, with the forged manifest.
    subprocess.run(
        ["git", "-C", str(clean_repo), "read-tree", evidence_commit], env=env, check=True
    )
    for rel in drop_from_e:
        subprocess.run(
            ["git", "-C", str(clean_repo), "update-index", "--force-remove", rel],
            env=env,
            check=True,
        )
    e_manifest = json.loads(
        _cat_blob(clean_repo, f"{evidence_commit}:{gate._MANIFEST_REL}")
    )
    forged_manifest = mutate_e_manifest(dict(e_manifest))
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            (
                "100644,"
                f"{_hash_blob(clean_repo, json.dumps(forged_manifest).encode('utf-8'))},"
                f"{gate._MANIFEST_REL}"
            ),
        ],
        env=env,
        check=True,
    )
    e2_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    e2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            e2_tree,
            "-p",
            tested_sha,
            "-m",
            "forged evidence commit",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # P2 tree = E2's tree with the report + manifest bound to E2.
    subprocess.run(
        ["git", "-C", str(clean_repo), "read-tree", e2], env=env, check=True
    )
    p_report = json.loads(_cat_blob(clean_repo, f"{p_sha}:{gate._REPORT_REL}"))
    p_report["evidence_commit"] = e2
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            (
                "100644,"
                f"{_hash_blob(clean_repo, json.dumps(p_report).encode('utf-8'))},"
                f"{gate._REPORT_REL}"
            ),
        ],
        env=env,
        check=True,
    )
    p_manifest = dict(forged_manifest)
    p_manifest["evidence_commit"] = e2
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            (
                "100644,"
                f"{_hash_blob(clean_repo, json.dumps(p_manifest).encode('utf-8'))},"
                f"{gate._MANIFEST_REL}"
            ),
        ],
        env=env,
        check=True,
    )
    p2_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    p2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            p2_tree,
            "-p",
            e2,
            "-m",
            "forged pointer commit",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            p2,
            p_sha,
        ],
        check=True,
    )
    return e2, p2


def _forge_pointer_tamper(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    tmp_path: Path,
    *,
    mutate_index: Callable[[Path, dict[str, str]], None],
) -> tuple[str, str]:
    """Publish a real trail, then forge a P2 whose tree is E's tree with P's
    report/manifest (still bound to E) plus whatever ``mutate_index`` applies to
    a temp index seeded from E, and repoint the gate ref at P2.

    ``mutate_index(repo, env)`` mutates the temp index (``GIT_INDEX_FILE``) —
    adding an extra blob, re-staging a tampered manifest, changing a committed
    evidence blob, etc.  The consumer resolver sees a coherent P2 -> E -> S chain
    whose violation is exactly the tree difference the counter-example targets
    (C-122 HG-H).  Returns (evidence_commit, forged_p2).
    """
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    index = tmp_path / "pointer-tamper-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(
        ["git", "-C", str(clean_repo), "read-tree", evidence_commit],
        env=env,
        check=True,
    )
    # Restore P's report/manifest (bound to E) so the pointer still binds E —
    # the ONLY intended difference from E is the tamper the test applies.
    for rel in (gate._REPORT_REL, gate._MANIFEST_REL):
        data = _cat_blob(clean_repo, f"{p_sha}:{rel}")
        subprocess.run(
            [
                "git",
                "-C",
                str(clean_repo),
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{_hash_blob(clean_repo, data)},{rel}",
            ],
            env=env,
            check=True,
        )
    mutate_index(clean_repo, env)
    p2_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    p2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            p2_tree,
            "-p",
            evidence_commit,
            "-m",
            "forged pointer commit",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            p2,
            p_sha,
        ],
        check=True,
    )
    return evidence_commit, p2


def test_verify_gate_ref_rejects_merge_pointer_commit(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    tmp_path: Path,
) -> None:
    """C-122 HG-C counter-example: a MERGE commit under the gate namespace — first
    parent E correct, second parent foreign — must fail closed as a non-single-
    parent chain even though the first-parent reads resolve P->E->S."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    index = tmp_path / "merge-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(
        ["git", "-C", str(clean_repo), "read-tree", p_sha], env=env, check=True
    )
    merge_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    merge_p = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            merge_tree,
            "-p",
            evidence_commit,
            "-p",
            tested_sha,
            "-m",
            "merged pointer",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            merge_p,
            p_sha,
        ],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("single-parent" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_wrong_manifest_size_bytes(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: E's manifest records a committed file's
    size_bytes that does not match the actual blob size — the consumer must fail
    closed instead of trusting the manifest's recorded size."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["committed"]:
                entry["size_bytes"] = int(entry["size_bytes"]) + 12345
                break
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("size_bytes" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_wrong_manifest_sha256(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 2 counter-example (wrong hash): E's manifest
    records a committed file's sha256 that does not recompute to the ACTUAL blob
    E committed — the consumer must fail closed instead of trusting the manifest's
    recorded digest."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["committed"]:
                entry["sha256"] = "b" * 64
                break
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("sha256" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_relocated_compact_manifest_entry(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 2 counter-example (relocated): E's manifest
    keeps a compact's canonical NAME but rewrites its tracked_path to a different
    repo path — the name set and every field shape still read correctly, but the
    per-name name→tracked_path→committed contract must fail closed."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["name"] == gate._COMPACT_CANARY_STAGED_NAME:
                entry["tracked_path"] = "benchmarks/results/moved-layer5-compact.json"
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "tracked_path" in problem and "relocated" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_raw_committed_flag_flipped(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 2 counter-example (raw committed flipped): a
    git-ignored raw evidence file (live-*) whose manifest entry is flipped to
    committed=true — the canonical committed flag for every fixed name is fixed
    (raws committed=false, everything else true), so the flip must fail closed."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["name"] == "live-canary-certified.json":
                entry["committed"] = True
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "committed" in problem and "flipped" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_committed_file_flipped_to_raw(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 01:10 Block 2 counter-example (committed flipped to raw):
    a fixed committed file (product-acceptance.json) whose manifest entry is
    flipped to committed=false — a committed artifact can never masquerade as a
    hash-only raw origin.  The canonical committed flag must hold for every name."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["name"] == "product-acceptance.json":
                entry["committed"] = False
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "committed" in problem and "flipped" in problem
        for problem in verdict["problems"]
    )


def _pointer_commit_preflight_report(
    clean_repo: Path, tested_sha: str
) -> gate.GateReport:
    """A report that binds the same tested revision / run id a forged P2's
    committed report carries, so ``_verify_pointer_committed_blobs`` reaches the
    manifest-contract checks instead of failing on a binding mismatch."""
    return gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )


def test_verify_pointer_committed_blobs_rejects_relocated_manifest_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 03:46 (Block 2) counter-example: the P publish preflight
    (``_verify_pointer_committed_blobs``) must enforce the SAME canonical
    manifest contract the resolver enforces — a forged P2 whose manifest relocates
    a compact's ``tracked_path`` off the canonical contract must be rejected at
    the preflight, not certified and only later caught by ``verify_gate_ref``."""
    e2, p2 = _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=_gap3_manifest_relocate_entry,
    )
    report = _pointer_commit_preflight_report(clean_repo, _head(clean_repo))
    with pytest.raises(
        gate.GateStateChangedError, match=r"tracked_path .* \(relocated\)"
    ):
        gate._verify_pointer_committed_blobs(
            p2,
            report,
            e2,
            gate._evidence_index_entries(
                staging_dir,
                report_stage=staging_dir / gate._REPORT_STAGED_NAME,
                manifest_stage=staging_dir / gate._MANIFEST_STAGED_NAME,
            ),
            staging_dir,
        )


def test_verify_pointer_committed_blobs_rejects_committed_flag_flipped(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 03:46 (Block 2) counter-example: the P publish preflight
    must reject a forged P2 whose manifest flips a fixed committed file's
    ``committed`` flag — a committed artifact can never masquerade as a hash-only
    raw origin in P, just as the resolver rejects it."""
    e2, p2 = _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=_gap3_manifest_flip_committed,
    )
    report = _pointer_commit_preflight_report(clean_repo, _head(clean_repo))
    with pytest.raises(gate.GateStateChangedError, match="committed flag flipped"):
        gate._verify_pointer_committed_blobs(
            p2,
            report,
            e2,
            gate._evidence_index_entries(
                staging_dir,
                report_stage=staging_dir / gate._REPORT_STAGED_NAME,
                manifest_stage=staging_dir / gate._MANIFEST_STAGED_NAME,
            ),
            staging_dir,
        )


def test_verify_gate_ref_rule_drift_does_not_flip_verification(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (规则漂移): after a CORRECT trail
    is published, editing the worktree ``.gitignore`` so a committed evidence
    file (done-gate-layer6-compact.json) is now ignored must NOT flip the
    verification — the canonical committed flag is the fixed authoritative
    contract, never a re-read of the current ignore rule."""
    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=lambda manifest: manifest,
    )
    # Rule drift AFTER the trail is published: ignore the previously-committed
    # layer-6 compact.  The published S/E/P trail is unchanged.
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n"
        "/benchmarks/results/done-gate-layer6-compact.json\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "drift ignore rule"],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is True


def test_verify_gate_ref_rule_drift_cannot_hide_forged_committed_flag(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 supervision 18:13 counter-example (规则漂移 attack): a forged
    manifest that lists the layer-6 compact as committed=false must fail closed
    EVEN WHEN the worktree ``.gitignore`` is edited to agree with the forgery —
    the committed flag comes from the fixed contract (committed=True for the
    compacts), so rule drift can never launder a forged committed flag into a
    passing verification."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["name"] == gate._COMPACT_E2E_STAGED_NAME:
                entry["committed"] = False
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    # Rule drift agreeing with the forgery: the layer-6 compact is now ignored.
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n"
        "/benchmarks/results/done-gate-layer6-compact.json\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "drift ignore rule"],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "committed" in problem and "flipped" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_missing_required_compact(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: a trail whose E is missing a committed layer-5/6
    compact artifact (the manifest still lists it) is verified=False — a
    committed=false raw hash must never be the only layer-5/6 evidence."""
    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=lambda manifest: manifest,
        drop_from_e=(
            "benchmarks/results/done-gate-layer5-compact.json",
        ),
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "compact" in problem and "missing" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_gitignored_raw_leaked_into_e(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: a raw live-* evidence file the manifest records
    as committed=false must NOT exist in E's tree — a raw origin that leaked into
    the object graph is a contract violation even when the manifest says it was
    never committed."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    index = tmp_path / "leak-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(
        ["git", "-C", str(clean_repo), "read-tree", evidence_commit], env=env, check=True
    )
    raw_rel = "benchmarks/results/live-canary-certified.json"
    raw_blob = _hash_blob(
        clean_repo,
        (staging_dir / "live-canary-certified.json").read_bytes(),
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{raw_blob},{raw_rel}",
        ],
        env=env,
        check=True,
    )
    e2_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    e2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            e2_tree,
            "-p",
            tested_sha,
            "-m",
            "forged E with raw leak",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "read-tree",
            e2,
        ],
        env=env,
        check=True,
    )
    p_report = json.loads(_cat_blob(clean_repo, f"{p_sha}:{gate._REPORT_REL}"))
    p_report["evidence_commit"] = e2
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            (
                "100644,"
                f"{_hash_blob(clean_repo, json.dumps(p_report).encode('utf-8'))},"
                f"{gate._REPORT_REL}"
            ),
        ],
        env=env,
        check=True,
    )
    p_manifest = json.loads(_cat_blob(clean_repo, f"{evidence_commit}:{gate._MANIFEST_REL}"))
    p_manifest["evidence_commit"] = e2
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-index",
            "--add",
            "--cacheinfo",
            (
                "100644,"
                f"{_hash_blob(clean_repo, json.dumps(p_manifest).encode('utf-8'))},"
                f"{gate._MANIFEST_REL}"
            ),
        ],
        env=env,
        check=True,
    )
    p2_tree = subprocess.run(
        ["git", "-C", str(clean_repo), "write-tree"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    p2 = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            p2_tree,
            "-p",
            e2,
            "-m",
            "forged pointer with raw leak",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "update-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            p2,
            p_sha,
        ],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "committed=false" in problem and "carries it" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_raw_evidence_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: a layer-5 compact's raw_evidence.sha256 must
    equal the hash the E manifest records for the raw file — a compact certifying
    different raw bytes than the trail recorded fails closed."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        for entry in manifest["files"]:  # type: ignore[index]
            if entry["name"] == "live-canary-certified.json":
                entry["sha256"] = "b" * 64
                break
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "raw_evidence.sha256" in problem for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_credential_field_in_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: a committed E manifest carrying a credential
    field name is a leak even when every other binding is correct — the consumer
    re-scan of the committed JSON artifact must fail closed."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        manifest["session_token"] = "stale-token-shape"
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("credential field name" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_unknown_64hex_in_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-C counter-example: a committed E manifest carrying an opaque 64-hex
    value under a NON-digest key is a token-shaped leak — the consumer re-scan
    must fail closed (C-122 HG-F reserves only the explicit field-path whitelist)."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        manifest["opaque_token"] = "a" * 64
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("unknown 64-hex value" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_digest_named_unproduced_64hex_in_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-F counter-example: the consumer re-scan must reject a 64-hex under
    a digest-NAMED key (``custom_fingerprint``) that no committed artifact of this
    gate produces — a mere ``*_hash``/``*_digest``/``*_fingerprint`` key name is no
    longer an automatic allow."""
    def mutate(manifest: dict[str, object]) -> dict[str, object]:
        manifest["custom_fingerprint"] = "a" * 64
        return manifest

    _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("unknown 64-hex value" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_p_extra_blob(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-H counter-example: a pointer commit P that SMUGGLES an extra blob
    beyond the report/manifest re-stamp must fail closed — P is only allowed to
    differ from E in the two expected paths."""
    def mutate_index(repo: Path, env: dict[str, str]) -> None:
        blob = _hash_blob(repo, b'{"smuggled": true}')
        subprocess.run(
            [
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                f"100644,{blob},benchmarks/results/smuggled-extra.json",
            ],
            env=env,
            check=True,
        )

    _forge_pointer_tamper(
        monkeypatch, clean_repo, staging_dir, tmp_path, mutate_index=mutate_index
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("added unexpected path" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_credential_field_in_p_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-H counter-example: P's committed manifest carrying a credential
    field name is a leak — the consumer must re-scan P's manifest blob, not just
    E's manifest, so a hijacked pointer commit cannot hide a secret in its own
    manifest."""
    def mutate_index(repo: Path, env: dict[str, str]) -> None:
        p_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"refs/tripchord/done-gate/{_TEST_RUN_ID}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        manifest = json.loads(_cat_blob(repo, f"{p_sha}:{gate._MANIFEST_REL}"))
        manifest["session_token"] = "stale-token-shape"
        blob = _hash_blob(
            repo, json.dumps(manifest, sort_keys=True).encode("utf-8")
        )
        subprocess.run(
            [
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{gate._MANIFEST_REL}",
            ],
            env=env,
            check=True,
        )

    _forge_pointer_tamper(
        monkeypatch, clean_repo, staging_dir, tmp_path, mutate_index=mutate_index
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("credential field name" in problem for problem in verdict["problems"])


def test_verify_gate_ref_rejects_p_manifest_unbound_file_entries(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 round-18 HG-H2 (supervision 16:03) counter-example: a forged pointer
    commit P whose manifest records a WELL-FORMED but unbound file contract —
    every file carrying the same arbitrary 64-hex sha256, size 0, and
    committed=false — must fail closed.  P's manifest entries must be EXACTLY the
    evidence E canonically committed (same tracked_path/committed/sha256/size
    and, for committed entries, the real blob hash), not merely pass the field
    shape contract."""
    def mutate_index(repo: Path, env: dict[str, str]) -> None:
        p_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"refs/tripchord/done-gate/{_TEST_RUN_ID}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        manifest = json.loads(_cat_blob(repo, f"{p_sha}:{gate._MANIFEST_REL}"))
        arbitrary = "a" * 64
        for entry in manifest["files"]:
            entry["sha256"] = arbitrary
            entry["size_bytes"] = 0
            entry["committed"] = False
        blob = _hash_blob(
            repo, json.dumps(manifest, sort_keys=True).encode("utf-8")
        )
        subprocess.run(
            [
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{gate._MANIFEST_REL}",
            ],
            env=env,
            check=True,
        )

    _forge_pointer_tamper(
        monkeypatch, clean_repo, staging_dir, tmp_path, mutate_index=mutate_index
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any(
        "E canonical manifest" in problem or "committed blob" in problem
        for problem in verdict["problems"]
    )


def test_verify_gate_ref_rejects_p_tree_diff_non_allowed_path(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path, tmp_path: Path
) -> None:
    """C-122 HG-H counter-example: a pointer commit P that MUTATES a committed
    evidence file (not the report/manifest) must fail closed — the P↔E tree diff
    may only ever touch the report/manifest re-stamp."""
    def mutate_index(repo: Path, env: dict[str, str]) -> None:
        rel = "benchmarks/results/done-gate-layer6-compact.json"
        # Read the current E-committed blob via the pointer's parent.
        parent = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"refs/tripchord/done-gate/{_TEST_RUN_ID}^"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        original = json.loads(_cat_blob(repo, f"{parent}:{rel}"))
        # Drop the frozen per-pair breakdown: a silent mutation of committed
        # evidence that must never ride into P behind the report/manifest.
        for check in original["done_gate"]["checks"]:
            if check["name"] == "v4_source_graph":
                check["evidence"].pop("per_pair", None)
        blob = _hash_blob(repo, json.dumps(original, sort_keys=True).encode("utf-8"))
        subprocess.run(
            [
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{rel}",
            ],
            env=env,
            check=True,
        )

    _forge_pointer_tamper(
        monkeypatch, clean_repo, staging_dir, tmp_path, mutate_index=mutate_index
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("changed E's committed file" in problem for problem in verdict["problems"])


def test_clean_env_launcher_scrubs_secrets(tmp_path: Path) -> None:
    """C-122 round-18 gate-8: the out-of-process clean-env launcher removes
    every secret-bearing variable BEFORE the child pytest process starts and
    keeps non-secret runtime variables untouched — no real credential can reach
    a test fixture frame or traceback."""
    import importlib.util

    launcher_path = (
        Path(gate.__file__).resolve().parent / "tests" / "run_tests_clean_env.py"
    )
    spec = importlib.util.spec_from_file_location(
        "run_tests_clean_env", str(launcher_path)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scrubbed = module._scrub_secrets(
        {
            "OPENAI_API_KEY": "sk-real",
            "TRIPCHORD_BROWSER_BRIDGE_TOKEN": "real-token",
            "COOKIE": "real-cookie",
            "PATH": "/usr/bin",
            "DATABASE_URL": "sqlite+aiosqlite:///./live.db",
            "VIRTUAL_ENV": "/opt/venv",
        }
    )
    assert "OPENAI_API_KEY" not in scrubbed
    assert "TRIPCHORD_BROWSER_BRIDGE_TOKEN" not in scrubbed
    assert "COOKIE" not in scrubbed
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["DATABASE_URL"] == "sqlite+aiosqlite:///./live.db"
    assert scrubbed["VIRTUAL_ENV"] == "/opt/venv"


def _minimal_evidence_commit_args(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> tuple[gate.GateReport, gate.GitSnapshot, str]:
    """Shared setup for a valid ``_commit_evidence`` call: full production-faithful
    passing evidence in staging (raw layer-5/6 evidence plus the derived layer-5/6
    compact artifacts, with the repo's real ``live-*`` ignore rule and a persisted
    bridge-state file) and a clean-start snapshot.  Returns (report, start,
    tested_sha)."""
    _patch_root(monkeypatch, clean_repo)
    # Reproduce the real repository's rule: raw live-* evidence is gitignored, so
    # E/P carry it only by hash (committed=false) — the consumer-side contract
    # (verify_gate_ref) demands this exact production shape (C-122 HG-C).
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )
    # A persisted bridge-state file (docs/operations.md) so the layer-6 compact's
    # bridge-state lease bindings carry a valid sha256.  Lives out-of-repo so it
    # never dirties porcelain; the module-level snapshots are cleared so the
    # compact is derived from THIS test's file bytes, not a stale prior capture.
    bridge_state_path = staging_dir.parent / "bridge-state.json"
    bridge_state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(bridge_state_path))
    gate._BRIDGE_STATE_SNAPSHOT = None
    gate._BRIDGE_STATE_SNAPSHOT_AFTER = None
    # Full required raw evidence + the deterministic layer-5/6 compact artifacts.
    staging_dir.mkdir(exist_ok=True)
    _populate_required_evidence(staging_dir)
    gate._generate_compact_evidence(staging_dir)
    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)
    return report, start, tested_sha


def test_commit_evidence_writes_local_report_with_evidence_commit(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 7 + P0: the delivered local report copy is generated BEFORE the
    publish carrying evidence_commit=E (and the gate ref) — never re-dumped after
    the atomic ref creation.  HEAD stays at S; only the gate ref appears."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    out = staging_dir.parent / "delivered" / "report.json"
    evidence_commit = gate._commit_evidence(
        staging_dir, report, start=start, local_report_path=out
    )
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["evidence_commit"] == evidence_commit
    assert payload["evidence_commit"] != tested_sha
    assert payload["passed"] is True
    assert payload["gate_ref"] == f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    _assert_side_channel_published(clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit)


def test_commit_evidence_local_report_write_failure_is_not_silent(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 7 counter-example: if the delivered local report cannot be
    written, the commit phase FAILS CLOSED (HEAD never moves) instead of
    proceeding with a silently-missing local report."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    # The parent of the local report path is a regular file — mkdir fails.
    blocker = staging_dir.parent / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    out = blocker / "report.json"
    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(
            staging_dir, report, start=start, local_report_path=out
        )
    assert _head(clean_repo) == tested_sha


def test_commit_evidence_scans_local_report_before_cas(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 7 counter-example: a secret in the delivered report fails the
    commit BEFORE the CAS (HEAD unchanged) and the leaked local report copy is
    removed — the local report is scanned in known-E, not produced after the
    branch move."""
    token = "sk-bridge-0123456789abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("TRIPCHORD_BROWSER_BRIDGE_TOKEN", token)
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    report.summary = f"summary carries {token}"
    out = staging_dir.parent / "leaky-report.json"
    with pytest.raises(gate.GateStateChangedError, match="secret value"):
        gate._commit_evidence(
            staging_dir, report, start=start, local_report_path=out
        )
    assert _head(clean_repo) == tested_sha
    assert not out.exists()


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
    _populating_passing_layers(monkeypatch, staging_dir)
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
    _populating_passing_layers(monkeypatch, staging_dir)

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
    for attr, layer_name in (
        ("layer1_reproducibility", "1_reproducibility"),
        ("layer2_replay", "2_replay"),
        ("layer3_clean_chrome_fixtures", "3_clean_chrome_fixtures"),
        ("layer4_model_smoke", "4_model_smoke"),
        ("layer5_real_canary", "5_real_canary"),
    ):
        monkeypatch.setattr(
            gate,
            attr,
            lambda *args, layer_name=layer_name: gate.LayerResult(
                name=layer_name, passed=True
            ),
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


def test_main_rejects_existing_empty_staging_dir(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """C-118: an existing ``--staging-dir`` — even empty — is rejected with
    exit 2: the staging root must be created exclusively by this run so no
    pre-planted or stale file can ever be swept into the committed trail."""
    _patch_root(monkeypatch, clean_repo)
    empty = tmp_path / "staging-empty"
    empty.mkdir()
    before = _porcelain(clean_repo)
    rc = gate.main(["--staging-dir", str(empty), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before
    assert not (empty / "product-v1-done-gate.json").exists()


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


def test_commit_evidence_succeeds_when_head_moves_after_entry_snapshot(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: HEAD moving *after* the entry snapshot (a
    concurrent ``git commit`` landing while the gate builds E/P) does NOT disturb
    the publish.  E's parent is pinned to the tested revision S via
    ``commit-tree -p S``, never by re-reading the branch, so the concurrent
    commit survives untouched and only the gate ref appears — HEAD/branch/index/
    worktree are never rewritten."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    tested_sha = report.tested_commit_sha
    assert tested_sha is not None
    start = _expected_snapshot(clean_repo)
    # Concurrent writer moves HEAD after the entry snapshot.
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "--allow-empty", "-q", "-m", "moved"],
        check=True,
    )
    moved_sha = _head(clean_repo)
    assert moved_sha != tested_sha

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The publish succeeds and the concurrent commit is untouched: HEAD is the
    # concurrent writer's commit (never rewound to S, never advanced to P/E),
    # the tree is clean, and the gate ref carries P^=E, E^=S.
    assert _head(clean_repo) == moved_sha
    assert _porcelain(clean_repo) == ""
    _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit, expected_head=moved_sha
    )


def test_commit_evidence_succeeds_while_index_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: an active ``.git/index.lock`` (a concurrent git
    process mid-index-write) is IRRELEVANT to the side-channel publish — the gate
    never reads or writes the shared real index, so it must succeed without
    touching the lock, without moving the branch, and without disturbing the
    index/worktree.  Only the dedicated gate ref appears atomically."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    tested_sha = report.tested_commit_sha
    assert tested_sha is not None
    start = _expected_snapshot(clean_repo)
    index_lock = clean_repo / ".git" / "index.lock"
    index_lock.write_text("locked\n", encoding="utf-8")

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The publish succeeded while the shared index was locked: the branch never
    # moved, the concurrent holder's lock file survives untouched (the gate
    # never removed another process's lock), and the real index still matches
    # HEAD.  The only persistent change is the gate ref.
    assert _head(clean_repo) == tested_sha
    assert index_lock.exists()
    assert index_lock.read_text(encoding="utf-8") == "locked\n"
    _assert_side_channel_published(clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit)


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
    assert payload["gate_ref"] is None
    assert "evidence commit failed" in payload["summary"]
    # The repository was rolled back to a clean tree after the failure.
    assert _porcelain(clean_repo) == ""


def test_commit_evidence_skips_gitignored_evidence(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """Counter-example: the repository ignores ``benchmarks/results/live-*``, so
    the side-channel evidence commits must skip those targets (recorded by hash
    in the manifest as committed=false) while still carrying the committable
    evidence — E/P never claim to carry the ignored files, and the raw ignored
    originals stay in the exclusive staging dir."""
    _patch_root(monkeypatch, clean_repo)
    (clean_repo / ".gitignore").write_text(
        "/benchmarks/results/live-*\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(clean_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(clean_repo), "commit", "-q", "-m", "ignore live evidence"],
        check=True,
    )
    _populate_full_required_evidence(monkeypatch, staging_dir)

    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )

    # The ignored live-* evidence is NOT part of P's tree.
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", pointer_sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert not any("live-" in p for p in tree), f"ignored evidence committed: {tree}"
    # Committable evidence, report and manifest landed in P; the worktree is
    # untouched (nothing tracked was written).
    for rel in (
        "benchmarks/results/product-acceptance.json",
        "benchmarks/results/browser-e2e-screenshot.png",
        gate._REPORT_REL,
        gate._MANIFEST_REL,
    ):
        assert rel in tree, f"missing committed evidence path in P: {rel}"
    assert _porcelain(clean_repo) == ""
    assert not (clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json").exists()
    # The staging manifest records the ignored raw originals by hash only.
    manifest = json.loads(
        (staging_dir / gate._MANIFEST_STAGED_NAME).read_text(encoding="utf-8")
    )
    by_name = {entry["name"]: entry for entry in manifest["files"]}
    assert by_name["live-canary-certified.json"]["committed"] is False
    assert by_name["live-done-gate-v4.json"]["committed"] is False


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


def test_commit_evidence_publishes_binary_evidence_with_branch_ref_locked(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: a pre-existing lock on the PRODUCT branch ref
    (``refs/heads/main.lock``) is IRRELEVANT to the side-channel publish — the
    gate never updates the branch, only the namespaced ``refs/tripchord/done-gate/
    <run_id>``.  The binary PNG evidence is staged via the temp index (hash-object
    + update-index --cacheinfo, never a worktree write), carried in E/P, and the
    gate succeeds while the branch ref stays locked and untouched."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    # The binary PNG is the point of this counter-example — keep the specific
    # bytes so the committed blob readback is a byte-for-byte match.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 10
    (staging_dir / "browser-e2e-screenshot.png").write_bytes(png_bytes)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=_head(clean_repo),
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    tested_sha = report.tested_commit_sha
    assert tested_sha is not None
    start = _expected_snapshot(clean_repo)
    # Lock the product branch ref — under the old index-lock design git's own
    # update-ref CAS failed for real; under the side-channel design the gate
    # never touches this ref, so the publish must still succeed.
    branch = subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    branch_lock = clean_repo / ".git" / f"refs/heads/{branch}.lock"
    branch_lock.write_text("locked\n", encoding="utf-8")

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The publish succeeded while the branch ref was locked: HEAD/index/worktree
    # at S, the branch lock survives untouched, and P carries the binary PNG
    # byte-for-byte.
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    assert branch_lock.exists()
    assert branch_lock.read_text(encoding="utf-8") == "locked\n"
    png_rel = "benchmarks/results/browser-e2e-screenshot.png"
    committed_png = subprocess.run(
        ["git", "-C", str(clean_repo), "show", f"{pointer_sha}:{png_rel}"],
        capture_output=True,
        check=True,
    ).stdout
    assert committed_png == png_bytes


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
    _populate_full_required_evidence(monkeypatch, staging_dir)

    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    start = _expected_snapshot(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )

    # P contains the manifest + committable evidence, never the ignored live-*.
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", pointer_sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert gate._MANIFEST_REL in tree
    assert "benchmarks/results/product-acceptance.json" in tree
    assert not any("live-" in p for p in tree)
    # The manifest (staged + committed in P) records both committed and ignored
    # originals by hash.
    manifest = json.loads(
        (staging_dir / gate._MANIFEST_STAGED_NAME).read_text(encoding="utf-8")
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
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
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
        gate._verify_evidence_contract(
            e_commit, staging_dir, tested_commit_sha=tested_sha, run_id=report.run_id
        )


def test_verify_evidence_contract_fails_closed_on_missing_committed_file(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A2 counter-example: the contract verify must hard-fail when E carries the
    manifest but is missing a file the manifest marks committed."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
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
        gate._verify_evidence_contract(
            e_commit, staging_dir, tested_commit_sha=tested_sha, run_id=report.run_id
        )


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
    # isolate both the DB and the bridge-state preflights to a clean live state
    # (C-122 Fix 2: the bridge preflight binds the live .runtime file by default,
    # so an unpatched call would read the host's real bridge state).
    monkeypatch.setattr(gate, "_live_state_lease_preflight", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_bridge_state_lease_preflight", lambda *a, **k: [])
    # C-122 round-18 item 6: the post-run bridge-state postcheck is a fresh
    # read; this test exercises token propagation, not lease state, so both
    # lease reads are isolated to clean state.
    monkeypatch.setattr(gate, "_bridge_state_postcheck", lambda *a, **k: [])
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


def test_live_state_lease_preflight_negative_offset_future_lease_is_residual(
    tmp_path: Path,
) -> None:
    """C-122 round-18 counter-example: an aware lease with a NEGATIVE UTC offset
    whose wall clock is just in the past is actually still in the future once
    converted.  ``replace(tzinfo=UTC)`` relabels the wall clock and would MISS
    this residual; ``astimezone(UTC)`` must detect it."""
    db_path = tmp_path / "live.db"
    wall = datetime.now(UTC) - timedelta(hours=1)
    lease = wall.replace(tzinfo=timezone(timedelta(hours=-8))).isoformat()
    _make_jobs_db(db_path, [("job-neg", "queued", lease)])
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "job-neg" in residual[0]


def test_live_state_lease_preflight_positive_offset_past_lease_is_not_residual(
    tmp_path: Path,
) -> None:
    """C-122 round-18 counter-example: an aware lease with a POSITIVE UTC offset
    whose wall clock is just in the future is actually already past once
    converted.  ``replace(tzinfo=UTC)`` would falsely flag it residual (a safe
    false positive, but wrong); ``astimezone(UTC)`` must clear it."""
    db_path = tmp_path / "live.db"
    wall = datetime.now(UTC) + timedelta(hours=1)
    lease = wall.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
    _make_jobs_db(db_path, [("job-pos", "running", lease)])
    assert gate._live_state_lease_preflight(db_path) == []


def test_live_state_lease_preflight_naive_lease_fails_closed(
    tmp_path: Path,
) -> None:
    """C-122 round-18 gate-5 counter-example: a NAIVE lease (bare wall clock, no
    timezone) is ambiguous — ``astimezone(UTC)`` on a naive datetime silently
    relabels it as host-local, misjudging any non-UTC live state.  A lease
    without an explicit zone must be treated as residual (fail-closed), never
    cleared."""
    db_path = tmp_path / "live.db"
    # A naive lease one hour in the FUTURE wall-clock; if it were silently
    # relabelled host-local it would look unexpired and be mis-cleared.
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    _make_jobs_db(db_path, [("job-naive", "queued", naive)])
    residual = gate._live_state_lease_preflight(db_path)
    assert residual
    assert "job-naive" in residual[0]
    assert "naive" in residual[0].lower()


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
    """R7 integration: a clean live-state DB and an isolated clean bridge-state
    file pass the preflight and the layer proceeds to the E2E runner (which
    then drives the verdict)."""
    staging_dir.mkdir()
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [])
    bridge_state_path = tmp_path / "bridge-state.json"
    bridge_state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(bridge_state_path))
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
    _populating_passing_layers_without(monkeypatch, staging_dir, "live-done-gate-v4.json")

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
    _populating_passing_layers_without(
        monkeypatch, staging_dir, "live-canary-certified.json"
    )

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
    with pytest.raises(gate.GateStateChangedError, match=r"browser-e2e\.json"):
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
    canary["passed"] = False  # only 2 of the 7 certified canary scopes are present
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    compact = gate._compact_canary(staging_dir)
    assert compact is not None
    coverage = compact["coverage"]
    assert coverage["expected_scope_count"] == len(gate._ALL_CERTIFIED_CANARY_SCOPES)
    assert set(coverage["expected_scopes"]) == set(gate._ALL_CERTIFIED_CANARY_SCOPES)
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
    _populate_full_required_evidence(monkeypatch, staging_dir)
    blank_payload = '{"schema_version": "tripchord-done-gate-layer6-compact-v2"}\n'
    staged_compact = staging_dir / gate._COMPACT_E2E_STAGED_NAME
    staged_compact.write_text(blank_payload, encoding="utf-8")
    tested_sha = _head(clean_repo)
    manifest = {
        "schema_version": gate._MANIFEST_SCHEMA,
        "tested_commit_sha": tested_sha,
        "run_id": "test-run",
        "evidence_commit": tested_sha,
        "generated_at": "2026-08-10T00:00:00+00:00",
        "branch": "main",
        "files": gate._manifest_files(staging_dir),
        "layer_verdicts": {"5_real_canary": {}, "6_full_e2e": {}},
    }
    # E (== HEAD) carries the manifest plus every staged evidence file the
    # manifest records — mirroring the real commit flow — so the compact-content
    # check below is isolated from the file-set / committed-file checks.
    results = clean_repo / "benchmarks" / "results"
    results.mkdir(parents=True, exist_ok=True)
    for staged_name, tracked_rel in gate._EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if staged.is_file():
            (results / Path(tracked_rel).name).write_bytes(staged.read_bytes())
    (results / Path(gate._MANIFEST_REL).name).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    gate._git("add", "--", str(results), check=True)
    gate._git(
        "commit",
        "-q",
        "-m",
        "blank compact",
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="done-gate report"):
        gate._verify_evidence_contract(
            _head(clean_repo), staging_dir, tested_commit_sha=tested_sha,
            run_id="test-run",
        )


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

    head_before = _head(clean_repo)
    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])
    assert rc == 0

    # The compact artifacts are committed on the SIDE-CHANNEL pointer commit P,
    # never on the product branch: HEAD stays at the tested revision and the
    # dedicated gate ref names P.
    run_id = json.loads(
        (staging_dir / "product-v1-done-gate.json").read_text(encoding="utf-8")
    )["run_id"]
    pointer_sha = _publish_ref(clean_repo, run_id)
    assert pointer_sha is not None
    assert _head(clean_repo) == head_before
    tree = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "-r", "--name-only", pointer_sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in (
        "benchmarks/results/done-gate-layer5-compact.json",
        "benchmarks/results/done-gate-layer6-compact.json",
    ):
        assert rel in tree, f"compact artifact missing from P tree: {rel}"
    manifest = json.loads(
        (staging_dir / gate._MANIFEST_STAGED_NAME).read_text(encoding="utf-8")
    )
    by_name = {entry["name"]: entry for entry in manifest["files"]}
    assert by_name[gate._COMPACT_CANARY_STAGED_NAME]["committed"] is True
    assert by_name[gate._COMPACT_E2E_STAGED_NAME]["committed"] is True
    # The raw layer-5/6 files are still not committed (gitignored).
    assert by_name["live-canary-certified.json"]["committed"] is False
    assert by_name["live-done-gate-v4.json"]["committed"] is False
    # The compact artifacts are independently reviewable structured JSON.
    compact = json.loads(
        subprocess.run(
            ["git", "-C", str(clean_repo), "show",
             f"{pointer_sha}:benchmarks/results/done-gate-layer5-compact.json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
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
    _populate_full_required_evidence(monkeypatch, staging_dir)
    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
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
        gate._verify_evidence_contract(
            e_commit, staging_dir, tested_commit_sha=tested_sha, run_id=report.run_id
        )


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
                staging_dir, gate._SecretNeedles(("nope",))
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
        gate._secret_scan_staging(staging_dir, gate._SecretNeedles(secrets))


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


def test_files_are_0600_from_creation_even_under_umask_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 Fix 5 counter-example: evidence and report files are 0600 from the
    instant of creation — not ``write_text`` at umask defaults followed by a
    chmod.  With the umask cleared, a ``write_text``-style write would land at
    0644; the sealed fd writers must still land at 0600."""
    old_umask = os.umask(0)
    try:
        sealed = tmp_path / "sealed.bin"
        gate._write_sealed_bytes(sealed, b"secret", 0o600)
        assert sealed.stat().st_mode & 0o777 == 0o600

        atomic = tmp_path / "atomic.json"
        gate._write_atomic(atomic, '{"ok": true}')
        assert atomic.stat().st_mode & 0o777 == 0o600

        report = gate.GateReport(
            schema_version=gate.EVIDENCE_SCHEMA,
            generated_at="2026-08-10T00:00:00+00:00",
            tested_commit_sha="a" * 40,
            run_id="test-run",
            passed=False,
            summary="x",
        )
        dumped = tmp_path / "report.json"
        gate._dump(report, dumped)
        assert dumped.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)


def test_sealed_write_is_0600_even_under_restrictive_umask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-18 counter-example: under ``umask 0777`` a bare
    ``os.open(..., mode=0o600)`` would create a 0000 file the owner cannot even
    read.  The fd write must ``os.fchmod`` the exact mode so the evidence is
    0600 regardless of how restrictive the umask is."""
    old_umask = os.umask(0o777)
    try:
        sealed = tmp_path / "sealed.bin"
        gate._write_sealed_bytes(sealed, b"secret", 0o600)
        assert sealed.stat().st_mode & 0o777 == 0o600
        assert sealed.read_bytes() == b"secret"

        atomic = tmp_path / "atomic.json"
        gate._write_atomic(atomic, '{"ok": true}')
        assert atomic.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)


def test_restore_tracked_file_seals_0600(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 Fix 5 counter-example: a rollback-restored tracked evidence file is
    0600 from creation — never a umask-default copy of the committed blob."""
    _patch_root(monkeypatch, clean_repo)
    rel = "benchmarks/results/product-acceptance.json"
    (gate.ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
    (gate.ROOT / rel).write_text('{"old": true}', encoding="utf-8")
    gate._git("add", "--", rel, check=True)
    gate._git("commit", "-m", "seed evidence", check=True)
    (gate.ROOT / rel).write_text('{"dirty": true}', encoding="utf-8")

    old_umask = os.umask(0)
    try:
        gate._restore_tracked_file(rel)
    finally:
        os.umask(old_umask)
    restored = gate.ROOT / rel
    assert restored.read_text(encoding="utf-8") == '{"old": true}'
    assert restored.stat().st_mode & 0o777 == 0o600


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


def test_secret_scan_allows_canary_pending_authorization_prose(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 supervision 04:14 regression counter-example: ``authorization`` /
    ``cookie`` are also English words, so the canary's legitimate scope detail
    ``pending user authorization: no connected Companion declares provider
    'ctrip'; pair the Companion and keep the official OTA domains logged in,
    then re-run`` is PROSE, not a header leak — the structured-JSON scan must
    not abort the gate on it.  (A real leak is a header FIELD — line start /
    JSON value position — which ``test_secret_scan_flags_authorization_header``
    and ``test_secret_scan_rejects_short_whole_header_forms_in_structured_json``
    still catch.)"""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(
            {
                "scopes": [
                    {
                        "scope": "ctrip:flight",
                        "authorized": False,
                        "detail": (
                            "pending user authorization: no connected Companion "
                            "declares provider 'ctrip'; pair the Companion and "
                            "keep the official OTA domains logged in, then re-run"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    gate.run_gate(staging_dir)  # must not raise


def test_secret_scan_rejects_short_whole_header_forms_in_structured_json(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 supervision 04:14 counter-example for the structured-JSON scan:
    the generic (non-``.failure.json``) evidence scan must still fail the gate
    closed on a whole Authorization/Cookie/X-API-Key header FIELD with a short
    (3-char) or quoted value — ``Cookie:a=b`` / ``X-API-Key:abc`` /
    ``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc; HttpOnly"`` /
    ``X-API-Key: "abc123"`` — when the header sits at a field position (line
    start / JSON value start), even though the prose in
    ``test_secret_scan_allows_canary_pending_authorization_prose`` passes."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"request": {"headers": "Cookie:a=b X-API-Key:abc '
        'Authorization: \\"Basic YWJjZA==\\" Set-Cookie: \\"sid=abc; HttpOnly\\" '
        'X-API-Key: \\"abc123\\""}}',
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
        gate.run_gate(staging_dir)


def test_secret_scan_rejects_quoted_key_json_dict_forms_in_failure_diagnostic(
    tmp_path: Path,
) -> None:
    """C-122 supervision 04:44 counter-example (final scan layer): even if a
    producer bypass were to write a JSON/dict QUOTED-KEY credential into a
    free-form diagnostic — double-quoted JSON keys (``failure={"Authorization":
    "Basic YWJjZA=="}``), single-quoted dict keys (``failure={'Set-Cookie':
    'sid=abc'}``), and quoted ``X-API-Key`` / ``Proxy-Authorization`` — the
    staging secret scan must fail the gate closed before the file is certified.
    ``json.dumps`` escapes the inner quotes, so the raw bytes also exercise the
    escaped-quote form (``{\\"Authorization\\":\\"Basic YWJjZA==\\"}``)."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    diag = staging_dir / "live-canary-certified.json.failure.json"
    diag.write_text(
        json.dumps(
            {
                "schema_version": "x",
                "summary": (
                    'failure={"Authorization":"Basic YWJjZA=="} '
                    "failure={'Set-Cookie': 'sid=abc'} "
                    'failure={"X-API-Key":"abc"} '
                    'failure={"Proxy-Authorization":"Bearer abcd"}'
                ),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="secret leak"):
        gate._secret_scan_staging(staging_dir, gate._SecretNeedles(()))


def test_secret_scan_rejects_quoted_key_forms_in_structured_json(
    tmp_path: Path,
) -> None:
    """C-122 supervision 04:44 counter-example (final scan layer, structured
    evidence): a committed-adjacent JSON artifact whose headers string carries
    QUOTED-KEY embedded JSON (``{\\"Authorization\\":\\"Basic a\\"}``) must fail
    the gate closed — the quoted field name is recognised at the escaped-quote
    field position, not only a bare ``Cookie:a=b`` form."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "live-done-gate-v4.json").write_text(
        '{"request": {"headers": "{\\"Authorization\\":\\"Basic a\\"}"}}',
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
        gate._secret_scan_staging(staging_dir, gate._SecretNeedles(()))


def test_canary_producer_desensitize_catches_quoted_key_json_dict_forms() -> None:
    """C-122 supervision 04:44 counter-example (stderr layer): the producer's
    ``_desensitize`` masks whole header fields even when the field name sits in
    a JSON/dict QUOTED-KEY position — double-quoted JSON keys
    (``{"Authorization": "Basic a"}``) and single-quoted dict keys
    (``{'Set-Cookie': 'sid=abc'}``) — while the legitimate business prose
    ``pending user authorization: ...`` is not a credential VALUE leak (only the
    keyword span is shape-masked)."""
    from benchmarks import live_canary_certified as canary

    for raw, forbidden in (
        ('failure={"Authorization":"Basic a"}', "Basic a"),
        ('failure={"Set-Cookie":"sid=abc"}', "sid=abc"),
        ('failure={"X-API-Key":"abc"}', "abc"),
        ("failure={'Authorization': 'Basic a'}", "Basic a"),
        ("failure={'Set-Cookie': 'sid=abc'}", "sid=abc"),
        ("failure={'X-API-Key': 'abc'}", "abc"),
        ('failure={"Proxy-Authorization":"Bearer abcd"}', "abcd"),
    ):
        out = canary._desensitize(raw)
        assert forbidden not in out, f"{forbidden!r} leaked in {out!r}"
        assert "[REDACTED]" in out, f"{raw!r} not masked in {out!r}"
    # Positive business prose is not a credential VALUE leak.
    prose = (
        "pending user authorization: no connected Companion declares "
        "provider 'ctrip'; pair the Companion and keep the official OTA "
        "domains logged in, then re-run"
    )
    out = canary._desensitize(prose)
    assert "pending user" in out


def test_canary_producer_seal_desensitizes_quoted_key_json_dict_forms(
    tmp_path: Path,
) -> None:
    """C-122 supervision 04:44 counter-example (producer artifact layer): the
    PRODUCER's own ``_seal_failure_diagnostic`` must not write a JSON/dict
    QUOTED-KEY credential into the ``<output>.failure.json`` ``summary`` — a
    double-quoted JSON ``Authorization``, a single-quoted dict ``Set-Cookie``
    and a quoted ``X-API-Key`` must NEVER appear raw in the committed
    diagnostic."""
    from benchmarks import live_canary_certified as canary

    output = tmp_path / "live-canary-certified.json"
    message = (
        'failure={"Authorization":"Basic YWJjZA=="} '
        "failure={'Set-Cookie': 'sid=abc; HttpOnly'} "
        'failure={"X-API-Key":"abc123"}'
    )
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(message),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    summary = json.loads(diag_path.read_text(encoding="utf-8"))["summary"]
    assert "YWJjZA==" not in summary
    assert "sid=abc" not in summary
    assert "abc123" not in summary
    assert "[REDACTED]" in summary


def test_canary_producer_seal_allows_pending_authorization_prose(
    tmp_path: Path,
) -> None:
    """C-122 supervision 04:44 positive counter-example (producer artifact
    layer): the business prose ``pending user authorization: ...`` is NOT a
    credential — the operational guidance that precedes the keyword survives the
    seal (only the ``authorization`` keyword span is conservatively masked,
    fail-closed direction), and the summary carries no credential VALUE."""
    from benchmarks import live_canary_certified as canary

    output = tmp_path / "live-canary-certified.json"
    message = (
        "pending user authorization: no connected Companion declares "
        "provider 'ctrip'; pair the Companion and keep the official OTA "
        "domains logged in, then re-run"
    )
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(message),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    summary = json.loads(diag_path.read_text(encoding="utf-8"))["summary"]
    assert "pending user" in summary


def test_layer5_redacts_quoted_key_json_dict_forms_from_final_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 supervision 04:44 counter-example (consumer / final report layer):
    the CONSUMER's sanitizer masks whole Authorization / Cookie / X-API-Key
    header fields even in a JSON/dict QUOTED-KEY position inside the failure
    summary — ``failure={"Authorization":"Basic YWJjZA=="}`` / ``failure=
    {'Set-Cookie': 'sid=abc'}`` / ``failure={"X-API-Key":"abc123"}`` — so the
    credentials never reach the committed layer-5 detail."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, "crashed"))
    evidence_path = staging_dir / "live-canary-certified.json"
    run_id = "x9y8z7w6v5u4"
    tested_sha = "a" * 40
    diag_path = _seal_canary_failure_diagnostic(
        evidence_path, run_id=run_id, tested_sha=tested_sha
    )
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
    diagnostic["summary"] = (
        'failure={"Authorization":"Basic YWJjZA=="} '
        "failure={'Set-Cookie': 'sid=abc; HttpOnly'} "
        'failure={"X-API-Key":"abc123"}'
    )
    diag_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    result = gate.layer5_real_canary(
        staging_dir, run_id=run_id, tested_commit_sha=tested_sha
    )
    assert result.passed is False
    diag_checks = [
        c for c in result.sub_checks if c.get("name") == "canary_failure_diagnostic"
    ]
    assert diag_checks, "a valid (this-run, fresh) diagnostic still keeps its classification"
    detail = diag_checks[0]["detail"]
    assert "YWJjZA==" not in detail
    assert "sid=abc" not in detail
    assert "abc123" not in detail
    assert "[REDACTED]" in detail


def _layered_json(secret_obj: dict, levels: int) -> str:
    """Build a ``json.dumps``-encoded chain ``levels`` deep.

    Each extra level wraps the previous JSON STRING as a value in a new dict and
    dumps again, so level N is what a credential smuggler lands when they
    ``json.dumps`` the diagnostic N times — each dump adds one backslash layer.
    """
    text = json.dumps(secret_obj)
    for _ in range(levels):
        text = json.dumps({"outer": text})
    return text


def test_canary_producer_desensitize_catches_double_triple_encoded_json() -> None:
    """C-122 supervision 06:58 counter-example (stderr / producer layer): the
    producer's ``_desensitize`` must mask a credential even when it is smuggled
    through MULTIPLE ``json.dumps`` layers — level 2 (double) and level 3
    (triple) encoded ``{"outer": "{\\"Authorization\\": \\"Basic YWJjZA==\\", …}"}``
    must never reach stderr or the sealed failure diagnostic."""
    from benchmarks import live_canary_certified as canary

    secret = {"Authorization": "Basic YWJjZA==", "Cookie": "a=b"}
    for level in (0, 1, 2, 3, 4):
        raw = _layered_json(secret, level)
        out = canary._desensitize(raw)
        assert "YWJjZA==" not in out, f"level {level} leaked base64: {out!r}"
        assert "a=b" not in out, f"level {level} leaked cookie value: {out!r}"
        assert "[REDACTED]" in out, f"level {level} not masked: {out!r}"


def test_canary_consumer_sanitize_catches_double_triple_encoded_json() -> None:
    """C-122 supervision 06:58 counter-example (consumer layer): the CONSUMER's
    ``_sanitize_canary_diag_field`` must re-mask a credential smuggled through
    multiple ``json.dumps`` layers (level 2 / 3 / 4) so it can never reach the
    committed layer-5 detail."""
    secret = {"Authorization": "Basic YWJjZA==", "Cookie": "a=b"}
    for level in (0, 1, 2, 3, 4):
        raw = _layered_json(secret, level)
        out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert "YWJjZA==" not in out, f"level {level} leaked base64: {out!r}"
        assert "a=b" not in out, f"level {level} leaked cookie value: {out!r}"
        assert "[REDACTED]" in out, f"level {level} not masked: {out!r}"


def test_secret_scan_rejects_double_triple_encoded_json() -> None:
    """C-122 supervision 06:58 counter-example (final scan layer): the bounded
    recursive JSON scan must fail the gate closed on a credential smuggled
    through multiple ``json.dumps`` layers — for BOTH a free-form
    ``.failure.json`` staging diagnostic (``credential_field_check=False``) and a
    committed JSON artifact (``credential_field_check=True``)."""
    secret = {"Authorization": "Basic YWJjZA==", "Cookie": "a=b"}
    for level in (0, 1, 2, 3, 4):
        raw = _layered_json(secret, level).encode()
        # .failure.json staging path — raw-byte + recursive pattern scan only.
        with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
            gate._secret_scan_bytes(
                raw,
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )
        # Committed JSON artifact path — includes the field-name rejector.
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw,
                gate._SecretNeedles(()),
                "committed evidence",
                f"evidence-{level}.json",
                credential_field_check=True,
            )


def test_secret_scan_rejects_malformed_nested_json_fail_closed() -> None:
    """C-122 supervision 06:58 counter-example: a structural-start string that
    does NOT parse — a truncated / obfuscated JSON attempt hiding a credential —
    must fail closed, never pass silently."""
    secret = {"Authorization": "Basic YWJjZA==", "Cookie": "a=b"}
    # The outermost json.dumps is VALID; the JSON-string VALUE is truncated
    # mid-value, so the recursive walker sees a malformed nested level.
    inner = json.dumps(secret)[:-4]  # chop ``a=b" }`` -> unterminated JSON
    raw = json.dumps({"summary": inner})
    with pytest.raises(gate.GateStateChangedError):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )


def test_secret_scan_rejects_depth_budget_overflow_fail_closed() -> None:
    """C-122 supervision 06:58 counter-example: a nested-JSON chain DEEPER than
    the hard depth budget must fail closed (``RecursiveJsonBudgetError``), never
    run unbounded recursion or silently pass."""
    from tripchord._secret_redact import _MAX_JSON_SCAN_DEPTH

    raw = json.dumps({"ok": 1})
    for _ in range(_MAX_JSON_SCAN_DEPTH + 3):
        raw = json.dumps({"outer": raw})
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "deep.json",
            credential_field_check=True,
        )
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._reject_credential_field_names(raw.encode(), b"x", "deep.json")


def test_secret_scan_rejects_structural_node_budget_overflow() -> None:
    """C-122 supervision 07:29 (gap 1) counter-example: the depth/node budgets
    must cover the JSON STRUCTURE itself — a 20000-primitive list inside one
    decoded level must fail closed (``budget exceeded``), never traverse without
    bound.  The same list as a mask input fails closed to ``[REDACTED]``."""
    from benchmarks import live_canary_certified as canary

    raw = json.dumps(list(range(20000)))
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "big.json",
            credential_field_check=True,
        )
    # Producer + consumer mask layers fail closed to the marker.
    assert canary._desensitize(raw) == "[REDACTED]"
    assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]"


def test_secret_scan_rejects_structural_depth_budget_overflow() -> None:
    """C-122 supervision 07:29 (gap 1) counter-example: a pathologically DEEP
    object (10 nested containers — beyond the depth=8 structural cap) must fail
    closed in scan AND mask, never rely on Python's recursion limit."""
    from benchmarks import live_canary_certified as canary

    node: Any = 1
    for _ in range(10):  # struct_depth 9 > 8
        node = {"k": node}
    raw = json.dumps(node)
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "deep.json",
            credential_field_check=True,
        )
    assert canary._desensitize(raw) == "[REDACTED]"
    assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]"


def test_secret_scan_rejects_fanout_empty_dict_budget_overflow() -> None:
    """C-122 supervision 07:29 (gap 1) counter-example: a FAN-OUT document
    (20000 empty-dict values) inside one decoded level must fail closed — the
    structural node cap counts every dict/list/scalar node, not just JSON-string
    levels."""
    raw = json.dumps({f"k{i}": {} for i in range(20000)})
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "fanout.json",
            credential_field_check=True,
        )


def test_secret_scan_allows_legit_structure_at_budget() -> None:
    """C-122 supervision 07:29 (gap 1) positive counter-example: a document that
    nests EXACTLY to the depth budget (9 containers -> struct_depth 8) and a
    normal small document both pass — the structural budget is a real cap, not a
    false positive on legitimate evidence."""
    node: Any = 1
    for _ in range(9):  # struct_depth 0..8 — at budget
        node = {"k": node}
    gate._secret_scan_bytes(
        json.dumps(node).encode(),
        gate._SecretNeedles(()),
        "evidence",
        "at-budget.json",
        credential_field_check=True,
    )  # must not raise
    gate._secret_scan_bytes(
        json.dumps({"a": 1, "b": [1, 2], "c": {"d": "text"}}).encode(),
        gate._SecretNeedles(()),
        "evidence",
        "normal.json",
        credential_field_check=True,
    )  # must not raise


def test_secret_scan_rejects_malformed_top_level_json() -> None:
    """C-122 supervision 07:29 (gap 2) counter-example: a ``.json`` evidence
    artifact (or any ``credential_field_check=True`` artifact) whose TOP-LEVEL
    text is UTF-8, looks like JSON and yet does NOT parse must fail closed —
    covering an ordinary truncation AND a unicode-escape + truncation attempt
    (``{"\\u12`` cut mid-escape).  Non-JSON text/binary and a valid non-sensitive
    JSON artifact pass through per the original contract."""
    for bad, name in (
        ('{"Authorization": "Basic', "live-done-gate-v4.json"),
        ('{"\\u12', "live-done-gate-v4.json"),
    ):
        with pytest.raises(gate.GateStateChangedError, match="malformed top-level JSON"):
            gate._secret_scan_bytes(
                bad.encode(),
                gate._SecretNeedles(()),
                "evidence",
                name,
                credential_field_check=False,
            )
        with pytest.raises(gate.GateStateChangedError, match="malformed top-level JSON"):
            gate._secret_scan_bytes(
                bad.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "evidence.json",
                credential_field_check=True,
            )
    # A plain non-JSON text / binary placeholder is NOT a malformed-JSON attempt
    # (original contract: byte + pattern scan only).
    gate._secret_scan_bytes(
        b"PNG",
        gate._SecretNeedles(()),
        "committed evidence",
        "browser-e2e-screenshot.png",
        credential_field_check=True,
    )  # must not raise
    gate._secret_scan_bytes(
        json.dumps({"ok": 1, "note": "public"}).encode(),
        gate._SecretNeedles(()),
        "evidence",
        "evidence.json",
        credential_field_check=True,
    )  # must not raise


def test_secret_scan_rejects_credential_keys_in_failure_artifact() -> None:
    """C-122 supervision 07:29 (gap 3) counter-example: the NORMALIZED key scan
    must run on free-form ``.failure.json`` diagnostics even when
    ``credential_field_check=False`` — a structured summary carrying a
    credential-looking KEY (``session_token`` / ``authorization_status`` /
    ``token``) is a leak the shape scans do NOT see (no word boundary before
    ``token``), so the key rejector is the only line that catches it."""
    for key in ("session_token", "authorization_status", "token"):
        raw = json.dumps({"summary": json.dumps({key: "abc"})})
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "committed evidence",
                "evidence.json",
                credential_field_check=True,
            )


def test_secret_scan_normalized_keys_unicode_and_both_check_paths() -> None:
    """C-122 supervision 07:29 (gap 3) positive + negative counter-examples: a
    legit outer document with a UNICODE key passes, a UNICODE credential key is
    still caught, and a TRUNCATED unicode key fails closed as malformed nested
    JSON — on BOTH the committed (True) and failure-artifact (False) paths."""
    # Legit outer + unicode key: passes on both paths.
    legit = json.dumps({"外层": {"note": "public", "count": 3}})
    gate._secret_scan_bytes(
        legit.encode(),
        gate._SecretNeedles(()),
        "committed evidence",
        "evidence.json",
        credential_field_check=True,
    )  # must not raise
    gate._secret_scan_bytes(
        legit.encode(),
        gate._SecretNeedles(()),
        "evidence",
        "live-canary-certified.json.failure.json",
        credential_field_check=False,
    )  # must not raise
    # Unicode credential key: still caught on both paths.
    bad = json.dumps({"外层": {"Authorization": "Basic YWJjZA=="}})
    with pytest.raises(gate.GateStateChangedError):
        gate._secret_scan_bytes(
            bad.encode(),
            gate._SecretNeedles(()),
            "committed evidence",
            "evidence.json",
            credential_field_check=True,
        )
    with pytest.raises(gate.GateStateChangedError):
        gate._secret_scan_bytes(
            bad.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )
    # Truncated unicode key at a NESTED level: malformed nested fail-closed.
    truncated = json.dumps({"summary": '{"\\u12'})
    with pytest.raises(gate.GateStateChangedError, match="malformed nested JSON"):
        gate._secret_scan_bytes(
            truncated.encode(),
            gate._SecretNeedles(()),
            "committed evidence",
            "evidence.json",
            credential_field_check=True,
        )
    with pytest.raises(gate.GateStateChangedError, match="malformed nested JSON"):
        gate._secret_scan_bytes(
            truncated.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )


def test_secret_scan_keeps_pending_authorization_and_cookie_prose() -> None:
    """C-122 supervision 06:58 positive counter-example: ``authorization`` /
    ``cookie`` are also English words, so the legitimate scope detail prose
    ``pending user authorization: …`` and ``the cookie: …`` (field name preceded
    by a SPACE / ordinary prose, not a header position) must NOT fail the scan —
    the field-position prefix guard (line start / quote / delimiter) still
    separates a real header FIELD from prose."""
    prose = (
        "pending user authorization: no connected Companion declares "
        "provider 'ctrip'; pair the Companion and re-run"
    )
    cookie_prose = "we use a cookie to remember your session"
    gate._secret_scan_bytes(
        prose.encode(),
        gate._SecretNeedles(()),
        "evidence",
        "prose.txt",
        credential_field_check=False,
    )  # must not raise
    gate._secret_scan_bytes(
        cookie_prose.encode(),
        gate._SecretNeedles(()),
        "evidence",
        "cookie-prose.txt",
        credential_field_check=False,
    )  # must not raise
    # The consumer sanitizer conservatively masks the ``authorization:`` keyword
    # SPAN but the prose PREFIX survives (fail-closed direction), while ``cookie``
    # with no ``:``/``=`` after it is plain English and passes through untouched —
    # the explicit ``authorization:``/``cookie:`` text-allowed boundary.
    sanitized = gate._sanitize_canary_diag_field(prose, "fallback")
    assert "pending user" in sanitized
    cookie_sanitized = gate._sanitize_canary_diag_field(cookie_prose, "fallback")
    assert cookie_sanitized == cookie_prose


def test_secret_scan_rejects_20000_string_values_budget_overflow() -> None:
    """C-122 supervision 00:06 (要求 A) counter-example: a document whose string
    VALUES alone exceed the node budget (20000 plain string values) must fail
    closed in scan AND mask — an ordinary/decoded string value counts toward the
    node budget, not just the JSON-string levels and containers."""
    from benchmarks import live_canary_certified as canary

    raw = json.dumps(["x"] * 20000)
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "many-strings.json",
            credential_field_check=True,
        )
    assert canary._desensitize(raw) == "[REDACTED]"
    assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]"


def test_secret_scan_rejects_20000_object_member_keys_budget_overflow() -> None:
    """C-122 supervision 00:06 (要求 A) counter-example: a 20000-member OBJECT
    fails closed — the object MEMBER KEY counts as a node (the member key AND
    the value both count), so a fan-out object cannot smuggle past the budget by
    hiding under a handful of JSON-string levels."""
    from benchmarks import live_canary_certified as canary

    raw = json.dumps({f"k{i}": i for i in range(20000)})
    with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "many-keys.json",
            credential_field_check=True,
        )
    assert canary._desensitize(raw) == "[REDACTED]"
    assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]"


def test_secret_scan_allows_exactly_at_node_budget_boundary() -> None:
    """C-122 supervision 00:06 (要求 A) positive counter-example: a document at
    EXACTLY the node budget (10000 nodes) passes the scan AND the mask layers —
    a 9998-item list of ints, a 9998-item list of plain strings and a 4999-member
    object each count exactly 10000 nodes; one more node (9999 ints / 5000
    members) fails closed.  The cap is a real ceiling, not a false positive on
    legal boundaries."""
    from benchmarks import live_canary_certified as canary

    for raw in (
        json.dumps(list(range(9998))),
        json.dumps(["x"] * 9998),
        json.dumps({f"k{i}": i for i in range(4999)}),
    ):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "at-budget.json",
            credential_field_check=True,
        )  # must not raise
        assert canary._desensitize(raw) != "[REDACTED]"
        assert gate._sanitize_canary_diag_field(raw, "fallback") != "[REDACTED]"
    for raw in (
        json.dumps(list(range(9999))),
        json.dumps({f"k{i}": i for i in range(5000)}),
    ):
        with pytest.raises(gate.GateStateChangedError, match="budget exceeded"):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "over-budget.json",
                credential_field_check=True,
            )
        assert canary._desensitize(raw) == "[REDACTED]"


def test_secret_scan_rejects_unicode_escaped_bearer_in_failure_diagnostic() -> None:
    """C-122 supervision 00:06 (要求 A) counter-example: a ``Bearer abcd``
    smuggled as unicode escapes (``\\u0042\\u0065...``) in a decoded
    ``.failure.json`` summary — the level TEXT never spells ``bearer`` (it sees
    the escaped form), but the DECODED string value is scanned one-by-one and is
    the plain credential, so the gate fails closed with the short-Bearer shape.
    """
    inner = '{"detail": "x \\u0042\\u0065\\u0061\\u0072\\u0065\\u0072 abcd"}'
    raw = json.dumps({"summary": inner})
    with pytest.raises(gate.GateStateChangedError, match="Bearer"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )


def test_secret_scan_rejects_short_opaque_token_assignment_in_failure() -> None:
    """C-122 supervision 00:06 (要求 A) counter-example: a short opaque
    ``token=abc`` assignment in a decoded ``.failure.json`` summary is a
    credential even though the value is only 3 chars.  C-122 supervision 09:59
    (Block 4): the legacy ``opaque_kv`` shape was removed and ``token`` is now a
    STRONG credential-FIELD name — the credential-field shape rejects it on the
    raw AND on the decoded-value scan."""
    raw = json.dumps({"summary": "token=abc"})
    with pytest.raises(
        gate.GateStateChangedError, match="credential field name assignment"
    ):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )


def test_secret_scan_rejects_fullwidth_zero_width_value_obfuscation() -> None:
    """C-122 supervision 00:06 (要求 B) counter-example: a credential VALUE
    obfuscated with full-width letters (``\uff21uthorization``) or a zero-width
    space (``Author\\u200bization``) is the same header as its ASCII form once
    the scan copy is NFKC + casefold + Cf/U+200B-dropped — rejected on BOTH the
    committed (credential_field_check=True) and failure-artifact (False) paths,
    in the raw value scan and at every decoded level."""
    full_width = json.dumps({"summary": "Ａuthorization: Basic YWJjZA=="})
    zero_width = json.dumps({"summary": "Author​ization: Basic YWJjZA=="})
    for raw in (full_width, zero_width):
        with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "committed evidence",
                "evidence.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )


def test_secret_scan_rejects_fullwidth_zero_width_credential_keys() -> None:
    """C-122 supervision 00:06 (要求 B) counter-example: a credential FIELD NAME
    obfuscated with full-width letters
    (``\uff33\uff45\uff53\uff53\uff49\uff4f\uff4e\uff3f\uff54\uff4f\uff4b\uff45\uff4e``)
    or a
    zero-width space (``Author\\u200bization``) is rejected by the NORMALIZED key
    scan on BOTH the committed and failure-artifact paths, while a legit outer
    document with a plain CJK key still passes."""
    full_width_key = json.dumps(
        {
            "summary": json.dumps(
                {
                    "Ｓｅｓｓｉｏｎ＿"
                    "ｔｏｋｅｎ": "abc"
                }
            )
        }
    )
    zero_width_key = json.dumps(
        {"summary": json.dumps({"Author​ization": "Basic YWJjZA=="})}
    )
    for raw in (full_width_key, zero_width_key):
        with pytest.raises(gate.GateStateChangedError, match="credential field name"):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "committed evidence",
                "evidence.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError, match="credential field name"):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )
    # A legit outer document with a plain CJK key still passes both paths.
    legit = json.dumps({"外层": {"note": "public", "count": 3}})
    gate._secret_scan_bytes(
        legit.encode(),
        gate._SecretNeedles(()),
        "committed evidence",
        "evidence.json",
        credential_field_check=True,
    )  # must not raise
    gate._secret_scan_bytes(
        legit.encode(),
        gate._SecretNeedles(()),
        "evidence",
        "live-canary-certified.json.failure.json",
        credential_field_check=False,
    )  # must not raise


def test_secret_scan_rejects_fullwidth_token_run_in_failure_decoded_value() -> None:
    """C-122 supervision 00:06 (要求 A/B) counter-example: a 32+ token run
    obfuscated as FULL-WIDTH letters (``\uff53\uff45\uff43\uff52\uff45\uff54`` =
    ``secret`` * 6 after NFKC) hidden in a decoded ``.failure.json`` value.  The
    level-TEXT scan only sees the ``\\uff53``-escaped form (invisible to
    ``token_run`` even on the normalized copy), so the DECODED value — where the
    real full-width characters surface — must apply the free-form FINAL_TEXT
    shape set, including the 32+ token run, and fail closed.  The same full-width
    run in a COMMITTED evidence
    value still PASSES: ``token_run`` is deliberately excluded from FINAL_VALUE
    there (committed artifacts legitimately carry 32+ ASCII runs) and the compact
    desensitizer masks the run instead of rejecting it."""
    fw_run = "ｓｅｃｒｅｔ" * 6  # 36 ASCII chars once NFKC-composed
    raw = json.dumps({"summary": fw_run})
    with pytest.raises(gate.GateStateChangedError, match="token-shaped run"):
        gate._secret_scan_bytes(
            raw.encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json.failure.json",
            credential_field_check=False,
        )
    gate._secret_scan_bytes(
        raw.encode(),
        gate._SecretNeedles(()),
        "committed evidence",
        "evidence.json",
        credential_field_check=True,
    )  # must not raise — committed-artifact token runs are masked, not rejected


def _credential_field_cases() -> list[tuple[str, str]]:
    """(label, raw) matrix of credential-FIELD-NAME assignment counter-examples.

    C-122 supervision 09:00 gap 2: structured (JSON key-value) AND free-text
    (``=`` / ``:``) forms, ASCII / full-width (NFKC) / zero-width (Cf) name
    spellings, short values (``abc`` — under the 32-char run threshold) and
    ``json.dumps`` nesting 1-3 levels.  Every row must be masked by the
    producer / consumer BEFORE disk and rejected by the final scan on BOTH the
    committed-evidence and free-form failure paths.
    """
    return [
        ("ascii quoted kv", 'Session_token:"abc"'),
        ("ascii free-text =", "Session_token=abc"),
        ("ascii colon space", "Session_token: abc"),
        ("fullwidth quoted kv", 'Ｓｅｓｓｉｏｎ_ｔｏｋｅｎ:"abc"'),
        ("fullwidth free-text =", "Ｓｅｓｓｉｏｎ_ｔｏｋｅｎ＝abc"),
        ("zero-width quoted kv", "Session​token:\"abc\""),
        ("zero-width free-text =", "Session​_token=abc"),
        ("zero-width colon space", "Session​token : abc"),
        ("structured ascii", json.dumps({"Session_token": "abc"})),
        ("structured zero-width", json.dumps({"Session​_token": "abc"})),
        ("nested 1-layer", json.dumps({"summary": json.dumps({"Session_token": "abc"})})),
        (
            "nested 2-layer",
            json.dumps(
                {"summary": json.dumps({"note": json.dumps({"Session_token": "abc"})})}
            ),
        ),
        (
            "nested 3-layer",
            json.dumps(
                {
                    "summary": json.dumps(
                        {"note": json.dumps({"detail": json.dumps({"Session_token": "abc"})})}
                    )
                }
            ),
        ),
        # C-122 supervision 09:28 (gap B): the credential-field parse/mask covers
        # from the FIRST non-empty value char to a clear field boundary or the
        # whole diagnostic — a 1-char / 2-char / space-separated / quoted /
        # full-width / zero-width value is masked WHOLE, never relying on a
        # 3-char token-run minimum.
        ("ascii 1-char", "Session_token=a"),
        ("ascii 2-char", "Session_token=ab"),
        ("ascii space-separated", "Session_token: abc def"),
        ("ascii quoted 1-char", 'session_token="a"'),
        ("ascii quoted 2-char", 'session_token="ab"'),
        ("fullwidth 1-char value", "Session_token=ａ"),
        ("zero-width 1-char value", "Session​token=a"),
        ("structured 1-char", json.dumps({"Session_token": "a"})),
        ("structured 2-char", json.dumps({"Session_token": "ab"})),
    ]


def test_gap2_producer_consumer_mask_credential_field_name_forms() -> None:
    """C-122 supervision 09:00 (gap 2) counter-example: the producer
    ``_desensitize`` and the consumer ``_sanitize_canary_diag_field`` mask a
    credential-FIELD-NAME assignment (``Session_token=abc``, ``Session_token:"abc"``,
    full-width / zero-width spellings, structured JSON key, 1-3 ``json.dumps``
    layers) WHOLE — the field name can never survive to stderr or the sealed
    failure diagnostic."""
    from benchmarks import live_canary_certified as canary

    for label, raw in _credential_field_cases():
        producer_out = canary._desensitize(raw)
        assert "session_token" not in producer_out.lower(), (
            f"producer leaked field name for {label}: {producer_out!r}"
        )
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert "session_token" not in consumer_out.lower(), (
            f"consumer leaked field name for {label}: {consumer_out!r}"
        )


def test_gap2_final_scan_rejects_credential_field_name_both_paths() -> None:
    """C-122 supervision 09:00 (gap 2) counter-example: the final scan rejects a
    credential-FIELD-NAME assignment on BOTH final paths — the committed
    evidence artifact (``credential_field_check=True``, raw top-level + decoded
    JSON-string values + the field-name rejector) and the free-form
    ``.failure.json`` diagnostic (FINAL_TEXT shape set)."""
    for label, raw in _credential_field_cases():
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "committed evidence",
                f"evidence-{label}.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )


def test_gap2_final_scan_rejects_bare_free_text_credential_field() -> None:
    """C-122 supervision 09:00 (gap 2) counter-example: a BARE free-text
    ``Session_token=abc`` (not valid JSON, does not sit inside a decoded string
    value) must still fail the COMMITTED path closed — the credential-FIELD
    shape runs on the raw top-level text, not only inside decoded JSON
    values."""
    for raw in ("Session_token=abc", "Ｓｅｓｓｉｏｎ_ｔｏｋｅｎ=abc", "Session​_token=abc"):
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "committed evidence",
                "evidence-1.json",
                credential_field_check=True,
            )


def test_gap1_unknown64_nested_decoded_json_path_reset() -> None:
    """C-122 supervision 09:00 (gap 1) counter-example: a 64-hex smuggled through
    a decoded JSON-string value (``{"summary": "{\\"files\\":[{\\"sha256\\":
    \\"<64hex>\\"}]}"}``, 1-3 ``json.dumps`` layers) must be REJECTED — the walk's
    decoded-level path resets to ``""``, so the nested ``files[].sha256`` must
    NOT hit the top-level digest whitelist.  A real top-level committed
    ``files[].sha256`` stays accepted; an UPPERCASE / full-width 64-hex is never
    trusted even under a whitelisted key."""
    hex64 = "a" * 64
    fw_hex = "ｆ" * 64

    def layered(value: str, levels: int) -> str:
        text = value
        for _ in range(levels):
            text = json.dumps({"outer": text})
        return text

    # Nested decoded JSON-string 64-hex — 1 / 2 / 3 layers — must fail closed.
    for level in (1, 2, 3):
        inner = json.dumps({"files": [{"sha256": hex64}]})
        with pytest.raises(gate.GateStateChangedError, match="64-hex"):
            gate._reject_unknown_64hex_values(
                layered(inner, level).encode(), b"x", f"nested-{level}.json"
            )
    # Full-width nested 64-hex fails closed too.
    with pytest.raises(gate.GateStateChangedError):
        gate._reject_unknown_64hex_values(
            json.dumps({"summary": json.dumps({"files": [{"sha256": fw_hex}]})}).encode(),
            b"x",
            "nested-fw.json",
        )
    # Top-level whitelisted path stays accepted.
    gate._reject_unknown_64hex_values(
        json.dumps({"files": [{"sha256": hex64}]}).encode(), b"x", "manifest.json"
    )
    # Uppercase / full-width 64-hex at a whitelisted path is never trusted.
    with pytest.raises(gate.GateStateChangedError):
        gate._reject_unknown_64hex_values(
            json.dumps({"files": [{"sha256": "A" * 64}]}).encode(),
            b"x",
            "upper.json",
        )
    with pytest.raises(gate.GateStateChangedError):
        gate._reject_unknown_64hex_values(
            json.dumps({"files": [{"sha256": fw_hex}]}).encode(),
            b"x",
            "fw.json",
        )


def test_gap1_gap2_positive_business_evidence_passes() -> None:
    """C-122 supervision 09:00 positive example: legitimate business state — a
    committed evidence manifest carrying real digest bindings at EXACT
    whitelisted paths and the ``bridge_token_present`` flag — passes the final
    scan unchanged, and a normal producer/consumer canary diagnostic passes
    through the mask layers without a false positive."""
    from benchmarks import live_canary_certified as canary

    hex64 = "a" * 64
    legit = {
        "files": [{"sha256": hex64}],
        "layer_verdicts": {
            "5_real_canary": {"companion": {"build_sha256": hex64}}
        },
        "done_gate": {
            "checks": [{"evidence": {"candidate_set_sha256": hex64}}]
        },
        "bridge_token_present": True,
    }
    raw = json.dumps(legit)
    # The full committed scan passes: no credential field name, no unknown 64-hex.
    gate._secret_scan_bytes(
        raw.encode(),
        gate._SecretNeedles(()),
        "committed evidence",
        "evidence-manifest.json",
        credential_field_check=True,
    )
    # Producer / consumer pass a normal business diagnostic unchanged — the
    # bounded walker may reorder JSON keys during its stack rebuild, so the
    # masked document must be JSON-equal to the input, never false-positive.
    business = json.dumps(
        {
            "summary": "flight search done",
            "query": {"origin": "ICOM_AIRPORT", "date": "2026-08-14"},
            "results": 2,
        }
    )
    assert json.loads(canary._desensitize(business)) == json.loads(business)
    assert json.loads(gate._sanitize_canary_diag_field(business, "fallback")) == (
        json.loads(business)
    )


def test_gap1_delimiter_in_key_never_whitelisted() -> None:
    """C-122 supervision 09:28 (gap A) counter-example: a 64-hex smuggled under a
    KEY that embeds path delimiters / array markers (``files[].sha256`` as a
    single key, ``files[]`` wrapping the ``sha256`` member, a dotted
    ``done_gate.checks[].evidence.candidate_set_sha256`` key) must be REJECTED —
    the digest whitelist matches TYPED key paths (string segments and array
    markers separated), never a dotted key segment.  A real committed path — a
    top-level ``api_payload_candidate_set_sha256`` / ``scenario_sha256`` scalar,
    the real ``files[].sha256`` nesting and the real ``done_gate.checks[].evidence.
    candidate_set_sha256`` nesting — stays accepted."""
    hex64 = "a" * 64
    for raw in (
        json.dumps({"files[].sha256": hex64}),
        json.dumps({"files[]": {"sha256": hex64}}),
        json.dumps({"done_gate.checks[].evidence.candidate_set_sha256": hex64}),
    ):
        with pytest.raises(gate.GateStateChangedError, match="64-hex"):
            gate._reject_unknown_64hex_values(raw.encode(), b"x", "delim.json")
    for raw in (
        json.dumps({"api_payload_candidate_set_sha256": hex64}),
        json.dumps({"scenario_sha256": hex64}),
        json.dumps({"files": [{"sha256": hex64}]}),
        json.dumps(
            {
                "done_gate": {
                    "checks": [{"evidence": {"candidate_set_sha256": hex64}}]
                }
            }
        ),
    ):
        gate._reject_unknown_64hex_values(raw.encode(), b"x", "real.json")


def test_gap1_digest_typed_path_never_aliased() -> None:
    """C-122 supervision 09:59 (Block 1) counter-example: the digest whitelist
    matches the ORIGINAL canonical spec key EXACTLY — a non-canonical alias of a
    whitelisted 64-hex field (uppercase ``API_PAYLOAD_CANDIDATE_SET_SHA256``,
    dash ``api-payload-candidate-set-sha256``, a trailing-space spelling) is a
    DIFFERENT typed path and is rejected, because a non-canonical alias is
    exactly where a foreign digest is smuggled in.  The compact validators
    reject an unknown top-level field for the same reason."""
    hex64 = "a" * 64
    for raw in (
        json.dumps({"API_PAYLOAD_CANDIDATE_SET_SHA256": hex64}),
        json.dumps({"api-payload-candidate-set-sha256": hex64}),
        json.dumps({"api_payload_candidate_set_sha256 ": hex64}),
        json.dumps({"Api_Payload_Candidate_Set_Sha256": hex64}),
    ):
        with pytest.raises(gate.GateStateChangedError, match="64-hex"):
            gate._reject_unknown_64hex_values(raw.encode(), b"x", "alias.json")
    # The ORIGINAL canonical key stays accepted.
    gate._reject_unknown_64hex_values(
        json.dumps({"api_payload_candidate_set_sha256": hex64}).encode(),
        b"x",
        "canonical.json",
    )
    # Compact validators: an ALIAS top-level field (uppercase / dash spelling of
    # a digest key) fails closed; the canonical top-level field set is allowed
    # by the unknown-field check (the full semantic validator still runs).
    l5 = {
        "schema_version": gate._LAYER5_COMPACT_SCHEMA,
        "generated_at": "x",
        "passed": True,
        "bridge_token_present": True,
        "coverage": {},
        "scopes": [],
        "companion_status": {},
        "raw_evidence": {},
    }
    l5_alias = dict(l5, **{"API_PAYLOAD_CANDIDATE_SET_SHA256": hex64})
    with pytest.raises(gate.GateStateChangedError, match="unknown top-level"):
        gate._verify_layer5_compact_contract("tracked", l5_alias)
    l6 = {
        "schema_version": gate._LAYER6_COMPACT_SCHEMA,
        "captured_at": "x",
        "run_status": "completed",
        "done_gate": {
            "passed": True,
            "check_count": 15,
            "passed_check_count": 15,
            "checks": [],
        },
        "repo_revision": "x",
        "start_revision": "x",
        "failure": None,
        "timeout_contract": {},
        "runner_contract": {},
        "event_injection_contract": {},
        "api_payload_candidate_set_sha256": hex64,
        "api_payload_sha256": hex64,
        "scenario_sha256": hex64,
        "runtime_before_run": {},
        "companion_preflight": {},
        "bridge_state_lease_preflight": {},
        "bridge_state_lease_postcheck": {},
        "raw_evidence": {},
    }
    l6_alias = dict(l6, **{"API_PAYLOAD_CANDIDATE_SET_SHA256": hex64})
    with pytest.raises(gate.GateStateChangedError, match="unknown top-level"):
        gate._verify_layer6_compact_contract("tracked", l6_alias)


def test_block2_duplicate_json_keys_fail_closed() -> None:
    """C-122 supervision 09:59 (Block 2) counter-example: a published JSON that
    repeats a whitelisted field name — a FOREIGN 64-hex first, then a normal
    value — must fail closed BEFORE any value is trusted.  ``json.loads`` keeps
    only the LAST value and discards the first, so both were previously
    accepted; every parser in the chain now loads through a canonical
    ``object_pairs_hook`` and rejects the duplicate key.  Top-level and nested
    1-3 layer JSON-string dupes all fail the committed scan, and the
    producer/consumer maskers collapse the document whole (fail closed)."""
    from benchmarks import live_canary_certified as canary

    foreign = "b" * 64
    normal = "c" * 64
    dup_top = (
        '{"api_payload_candidate_set_sha256": "' + foreign + '", '
        '"api_payload_candidate_set_sha256": "' + normal + '"}'
    )

    def layered(value: str, levels: int) -> str:
        text = value
        for _ in range(levels):
            text = json.dumps({"outer": text})
        return text

    for level in (0, 1, 2, 3):
        raw = dup_top if level == 0 else layered(dup_top, level)
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                f"dup-{level}.json",
                credential_field_check=True,
            )
        # The maskers fail closed — a duplicate key is a parse failure, never
        # keeping the last value: the whole top-level document (level 0) or the
        # dup JSON-string level collapses to the marker, so neither the foreign
        # nor the normal digest survives into the output.
        producer_out = canary._desensitize(raw)
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert foreign not in producer_out and normal not in producer_out
        assert foreign not in consumer_out and normal not in consumer_out
        assert "[REDACTED]" in producer_out
        assert "[REDACTED]" in consumer_out


def test_block3_charset_unrestricted_credential_field_values(tmp_path: Path) -> None:
    """C-122 supervision 09:59 (Block 3) counter-example: a non-empty credential
    field VALUE is masked from the FIRST non-empty character to a clear field
    boundary or the whole diagnostic, with NO ASCII charset limit — ``!`` / ``@``
    / ``/`` / CJK / emoji (``Session_token=\uff01/@/\u79d8\u5bc6/\U0001f511``),
    a ``Password=`` / ``passwd=`` value (``Password=a/ab/!``) and an
    ``@``-containing token (``Session_token=abc@def``) are masked WHOLE by the
    producer/consumer and rejected by both final scans; only the EXACT safe
    marker (``[REDACTED]``) is exempt; and a real 0600 seal-on-disk never leaks
    any of the forms."""
    from benchmarks import live_canary_certified as canary

    leak_free = [
        ("emoji-cjk", "Session_token=!/@/秘密/🔑", "session_token"),
        ("password-slash", "Password=a/ab/!", "password"),
        ("passwd-1char", "passwd=!", "passwd"),
        ("at-char", "Session_token=abc@def", "abc@def"),
        ("password-2char", "Password=ab", "password"),
    ]
    for label, raw, gone in leak_free:
        producer_out = canary._desensitize(raw)
        assert gone not in producer_out.lower(), (
            f"producer leaked {gone!r} for {label}: {producer_out!r}"
        )
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert gone not in consumer_out.lower(), (
            f"consumer leaked {gone!r} for {label}: {consumer_out!r}"
        )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )
    # A real seal-on-disk diagnostic never leaks any of the forms.
    output = tmp_path / "live-canary-certified.json"
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(
            "upstream 401 Session_token=!/@/秘密/🔑 Password=a/ab/! "
            "Session_token=abc@def"
        ),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    summary = json.loads(diag_path.read_text(encoding="utf-8"))["summary"]
    assert "session_token" not in summary.lower()
    assert "abc@def" not in summary
    assert "秘密" not in summary
    # The exact safe marker is the ONE exemption.
    assert canary._desensitize("secret=[REDACTED]") == "secret=[REDACTED]"
    assert gate._sanitize_canary_diag_field("secret=[REDACTED]", "fallback") == (
        "secret=[REDACTED]"
    )


def test_gap2_short_credential_values_to_field_boundary(tmp_path: Path) -> None:
    """C-122 supervision 09:28 (gap B) counter-example: the SHARED credential-field
    parse/mask covers from the FIRST non-empty value character to a clear field
    boundary or the whole diagnostic — a 1-char (``Session_token=a``), 2-char
    (``Session_token=ab``), space-separated (``Session_token: abc def`` — no
    ``def`` residue), quoted (``session_token=\"a\"``) or semicolon-bounded
    (``token=a; next=1``) value is masked WHOLE by the producer / consumer and
    rejected by both final scans; a real seal-on-disk diagnostic never leaks any
    of the short forms; and normal business prose (``we use a cookie jar``,
    ``secret=[REDACTED]``, ``pending user authorization: no connected
    Companion``) stays untouched."""
    from benchmarks import live_canary_certified as canary

    leak_free: list[tuple[str, str, str]] = [
        ("1-char", "Session_token=a", "session_token"),
        ("2-char", "Session_token=ab", "session_token"),
        ("space-separated", "Session_token: abc def", "def"),
        ("quoted 1-char", 'session_token="a"', "session_token"),
        ("quoted 2-char", 'session_token="ab"', "session_token"),
        ("semicolon 1-char", "token=a; next=1", "token=a"),
        ("semicolon 2-char", "token=ab; next=1", "token=ab"),
        ("fullwidth value", "Session_token=ａ", "session_token"),
        ("zero-width value", "Session​token=a", "session_token"),
        ("structured 1-char", json.dumps({"Session_token": "a"}), "session_token"),
        ("structured 2-char", json.dumps({"Session_token": "ab"}), "session_token"),
    ]
    for label, raw, gone in leak_free:
        producer_out = canary._desensitize(raw)
        assert gone not in producer_out.lower(), (
            f"producer leaked {gone!r} for {label}: {producer_out!r}"
        )
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert gone not in consumer_out.lower(), (
            f"consumer leaked {gone!r} for {label}: {consumer_out!r}"
        )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )

    # A real seal-on-disk diagnostic never leaks any of the short forms.
    output = tmp_path / "live-canary-certified.json"
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(
            "upstream 401 Session_token=a Session_token: abc def token=ab; next=1"
        ),
        output,
        run_id="abc123def456",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    summary = json.loads(diag_path.read_text(encoding="utf-8"))["summary"]
    assert "session_token" not in summary.lower()
    assert "def" not in summary
    assert "token=ab" not in summary

    # Normal business text passes through both mask layers unchanged — the
    # credential-FIELD shape must never flag an ordinary English sentence
    # (``cookie`` is a word, ``[REDACTED]`` is already a marker, ``token`` as a
    # plain word has no assignment).
    for prose in ("we use a cookie jar", "secret=[REDACTED]", "flight search done"):
        assert canary._desensitize(prose) == prose, f"producer masked prose {prose!r}"
        assert gate._sanitize_canary_diag_field(prose, "fallback") == prose, (
            f"consumer masked prose {prose!r}"
        )
    # The canary's own scope-detail PROSE (``authorization`` as an English word,
    # no token payload) stays allowed by the COMMITTED structured scan — it sits
    # inside a JSON string value, not at a header field position (the same
    # regression ``test_secret_scan_allows_canary_pending_authorization_prose``
    # covers).  The variants below are the exact detail strings the gate's own
    # ``product-v1-done-gate.json`` report carries — a real report must never
    # trip its own final scan.  The free-form producer / failure-path masks
    # still collapse them as a whole header by design — a cosmetic loss, never
    # a leak.
    for prose in (
        "pending user authorization: no connected Companion declares provider "
        "'ctrip'; pair the Companion and re-run",
        "pending user authorization: not all certified canary scopes have a "
        "fresh authorised read-only canary",
        "pending user authorization: full real E2E runs the configured mode",
    ):
        gate._secret_scan_bytes(
            json.dumps(
                {
                    "scopes": [
                        {"scope": "ctrip:flight", "authorized": False, "detail": prose}
                    ]
                }
            ).encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json",
            credential_field_check=True,
        )


def test_producer_consumer_mask_fullwidth_zero_width_credential_spans() -> None:
    """C-122 supervision 00:06 (要求 B) counter-example: the producer
    ``_desensitize`` and consumer ``_sanitize_canary_diag_field`` mask a
    full-width / zero-width-obfuscated credential span to ``[REDACTED]``, while
    the prose PREFIX survives — only the credential-carrying span is collapsed,
    never the whole message."""
    from benchmarks import live_canary_certified as canary

    for raw in (
        "Ａuthorization: Basic YWJjZA==",
        "Author​ization: Basic YWJjZA==",
        "Ｂｅａｒｅｒ abcd",
        "ｔｏｋｅｎ＝abc",
    ):
        producer_out = canary._desensitize(raw)
        assert producer_out == "[REDACTED]"
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert consumer_out == "[REDACTED]"
    # Prose prefix survives; only the credential span is masked.
    for raw in (
        "pending user Ａuthorization: Basic YWJjZA== end",
        "pending user Author​ization: Basic end",
    ):
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert "pending user" in consumer_out
        assert "[REDACTED]" in consumer_out


def test_r18_final_scan_rejects_complete_basic_auth_field_both_paths() -> None:
    """C-122 round-18 supervision re-review Block 1 counter-example: a complete
    ``Authorization``/``Proxy-Authorization`` ``Basic <base64>`` field that the
    producer/consumer already desensitize must ALSO fail the final scan's
    independent backstop on BOTH the committed-evidence and free-form failure
    paths — raw free text and JSON-wrapped decoded string values — while the
    three real ``pending user authorization:`` prose positives stay accepted by
    the committed structured scan."""
    from benchmarks import live_canary_certified as canary

    for raw in (
        "upstream Authorization: Basic YWJjZA==",
        "upstream proxy-authorization: Basic YWJjZA==",
        json.dumps({"summary": "upstream Authorization: Basic YWJjZA=="}),
        json.dumps({"detail": "upstream proxy-authorization: Basic YWJjZA=="}),
    ):
        # Committed evidence path (credential_field_check=True) and the
        # decoded-value scanner both reject the complete field.
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )
        # The producer masks the field whole; no base64 payload survives.
        assert "YWJjZA" not in canary._desensitize(raw)

    # The three real prose positives carry no ``Basic`` scheme and must stay
    # accepted by the committed structured scan (they sit inside JSON string
    # values in the real report, never at a header field position).
    for prose in (
        "pending user authorization: no connected Companion declares provider "
        "'ctrip'; pair the Companion and re-run",
        "pending user authorization: not all certified canary scopes have a "
        "fresh authorised read-only canary",
        "pending user authorization: full real E2E runs the configured mode",
    ):
        gate._secret_scan_bytes(
            json.dumps(
                {
                    "scopes": [
                        {"scope": "ctrip:flight", "authorized": False, "detail": prose}
                    ]
                }
            ).encode(),
            gate._SecretNeedles(()),
            "evidence",
            "live-canary-certified.json",
            credential_field_check=True,
        )


def test_r18_safe_marker_exact_exemption_rejects_trailing_chars() -> None:
    """C-122 round-18 supervision re-review Block 2 counter-example: the safe
    marker exemption is EXACT — only a field value that, after removing
    surrounding quotes, is precisely ``[REDACTED]`` AND immediately followed by a
    real field boundary / end is preserved.  Any trailing character
    (``[REDACTED]actual``, ``[REDACTED] actual``, the ``[REDACTED];def"`` residue
    of a quote-split value) fails the exemption and re-masks / re-rejects the
    WHOLE segment on all three layers."""
    from benchmarks import live_canary_certified as canary

    # Trailing characters must be masked whole by producer/consumer and rejected
    # by both final scans — never accepted as a "safe marker + junk".
    for raw in (
        "secret=[REDACTED]actual",
        "secret=[REDACTED] actual",
        'secret="[REDACTED]"actual',
        'secret=[REDACTED];def"',
        "token=[REDACTED]extra",
    ):
        assert canary._desensitize(raw) == "[REDACTED]", (
            f"producer left trailing residue for {raw!r}"
        )
        assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]", (
            f"consumer left trailing residue for {raw!r}"
        )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )

    # The EXACT marker (quoted or bare) followed by a clean boundary stays
    # untouched by producer/consumer and accepted by the committed final scan —
    # the gate's own redacted report detail must never trip its own scan.
    for raw in ("secret=[REDACTED]", 'secret="[REDACTED]"'):
        assert canary._desensitize(raw) == raw, f"producer masked exact marker {raw!r}"
        assert gate._sanitize_canary_diag_field(raw, "fallback") == raw, (
            f"consumer masked exact marker {raw!r}"
        )
    report_fragment = json.dumps({"detail": "secret=[REDACTED]", "name": "x"})
    gate._secret_scan_bytes(
        report_fragment.encode(),
        gate._SecretNeedles(()),
        "evidence",
        "product-v1-done-gate.json",
        credential_field_check=True,
    )


def test_r18_quote_aware_credential_value_no_seal_residue(tmp_path: Path) -> None:
    """C-122 round-18 supervision re-review Block 3 counter-example: the shared
    quote-aware credential-value parser masks a quoted / comma / backslash /
    bracket value WHOLE (``Session_token="abc;def"`` never leaves a ``;def"``
    residue, ``token=[1,2]`` never leaves ``,2]``), and a REAL 0600 seal written
    from a failure carrying such values is clean on disk — the consumer then
    masks the summary whole and the final scan rejects the raw forms on both
    paths."""
    from benchmarks import live_canary_certified as canary

    for raw in (
        'Session_token="abc;def"',
        "Session_token=abc,def",
        "Session_token=abc\\def",
        "token=[1,2]",
        "Session_token=abc@def",
        "Session_token=!/@/秘密/🔑",
        "Session_token: abc def",
    ):
        assert canary._desensitize(raw) == "[REDACTED]", (
            f"producer left residue for {raw!r}"
        )
        assert gate._sanitize_canary_diag_field(raw, "fallback") == "[REDACTED]", (
            f"consumer left residue for {raw!r}"
        )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )

    # A REAL 0600 seal written from a failure whose text carries the quote-split
    # values must be clean on disk — no ``;def"`` / ``,def`` / ``[1,2]`` residue
    # and no credential field name surviving in the summary.
    output = tmp_path / "live-canary-certified.json"
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(
            'upstream 401 Session_token="abc;def" Session_token=abc,def token=[1,2]'
        ),
        output,
        run_id="r18block3",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    summary = json.loads(diag_path.read_text(encoding="utf-8"))["summary"]
    assert "session_token" not in summary.lower()
    assert ";def\"" not in summary
    assert ",def" not in summary
    assert "[1,2]" not in summary
    assert "abc" not in summary
    # The consumer masks the sealed summary whole too — no residue survives.
    consumer_out = gate._sanitize_canary_diag_field(summary, "fallback")
    assert "session_token" not in consumer_out.lower()
    assert ";def\"" not in consumer_out
    assert "[1,2]" not in consumer_out


def test_r19_block16_semicolon_connected_credential_fields_no_plaintext(
    tmp_path: Path,
) -> None:
    """C-122 round-19 supervision re-review Block 16 counter-example: the
    credential-field VALUE-END boundary is NON-CONSUMING — a ``;``-connected
    second strong field (``token=[1,2];password=mySuperSecret123``,
    ``Session_token="abc;def";password=mySuperSecret123``,
    ``token=[1,2];secret='xyz123'``, ``token=[ab,cd];password=pw``) is masked
    SEPARATELY instead of the bracket/quoted value swallowing the ``;password=``
    and orphaning the second field's VALUE as plaintext.  Producer and consumer
    collapse the whole assignment, both final scans reject the raw leak form, and
    a REAL 0600 seal-on-disk never carries the plaintext."""
    from benchmarks import live_canary_certified as canary

    leak_inputs = [
        ("bracket-int", "token=[1,2];password=mySuperSecret123", "mySuperSecret123"),
        (
            "quoted-semicolon",
            'Session_token="abc;def";password=mySuperSecret123',
            "mySuperSecret123",
        ),
        ("bracket-secret", "token=[1,2];secret='xyz123'", "xyz123"),
        ("bracket-name", "token=[ab,cd];password=pw", "pw"),
    ]
    for label, raw, gone in leak_inputs:
        producer_out = canary._desensitize(raw)
        assert gone not in producer_out.lower(), (
            f"producer leaked {gone!r} for {label}: {producer_out!r}"
        )
        consumer_out = gate._sanitize_canary_diag_field(raw, "fallback")
        assert gone not in consumer_out.lower(), (
            f"consumer leaked {gone!r} for {label}: {consumer_out!r}"
        )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "ev.json",
                credential_field_check=True,
            )
        with pytest.raises(gate.GateStateChangedError):
            gate._secret_scan_bytes(
                raw.encode(),
                gate._SecretNeedles(()),
                "evidence",
                "live-canary-certified.json.failure.json",
                credential_field_check=False,
            )

    # A REAL 0600 seal written from a failure carrying the ;-connected strong
    # fields must be clean on disk — no second-field VALUE plaintext survives.
    output = tmp_path / "live-canary-certified.json"
    diag_path = canary._seal_failure_diagnostic(
        "evaluate",
        RuntimeError(
            "upstream 401 token=[1,2];password=mySuperSecret123 "
            'Session_token="abc;def";password=mySuperSecret123 '
            "token=[ab,cd];password=pw"
        ),
        output,
        run_id="r19block16",
        tested_sha="a" * 40,
    )
    assert diag_path.is_file()
    content = diag_path.read_text(encoding="utf-8")
    summary = json.loads(content)["summary"]
    assert "mySuperSecret123" not in content
    assert "xyz123" not in content
    assert ";password=" not in summary
    assert "[1,2]" not in summary
    assert "[ab,cd]" not in summary
    assert "abc;def" not in summary
    # The consumer re-masks the sealed summary whole too — no value residue.
    consumer_out = gate._sanitize_canary_diag_field(summary, "fallback")
    assert "mySuperSecret123" not in consumer_out
    assert ";password=" not in consumer_out


def test_secret_scan_rejects_double_encoded_authorization_in_structured_json(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 supervision 06:58 end-to-end counter-example: a double-encoded
    ``Authorization`` header smuggled inside a staged structured evidence file
    fails the gate even though the RAW bytes carry the value only as escaped
    JSON."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-canary-certified.json").write_text(
        _layered_json({"Authorization": "Basic YWJjZA==", "Cookie": "a=b"}, 2),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="Authorization/Cookie"):
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


def test_secret_scan_does_not_flag_hex_digest_with_phone_like_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 counter-example: a 64-hex sha256 whose digits happen to contain a
    phone-shaped run must NOT be flagged as a phone number — a computed digest
    is recomputable evidence, never an account identifier, and the same hash
    value in a report/manifest/compact must not make the regression flaky."""
    _patch_root(monkeypatch, tmp_path)
    phone_run = "13581234567"
    digest = "a" * 10 + phone_run + "c" * (64 - 10 - len(phone_run))
    assert len(digest) == 64
    file = tmp_path / "evidence.json"
    file.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    gate._secret_scan_paths([file], gate._SecretNeedles(()), "test")  # must not raise


def test_secret_scan_still_flags_real_phone_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 counter-example: masking recomputable hex digests must not hide a
    genuine bare Chinese mobile number in evidence — the phone scan still fires."""
    _patch_root(monkeypatch, tmp_path)
    file = tmp_path / "evidence.json"
    file.write_text(json.dumps({"contact": "13812345678"}), encoding="utf-8")
    with pytest.raises(gate.GateStateChangedError, match="phone number"):
        gate._secret_scan_paths([file], gate._SecretNeedles(()), "test")


def test_secret_needles_repr_never_exposes_values() -> None:
    """C-122 round-18 security contract: the scan needle container's repr must
    never expand the secret bytes, so a failing traceback cannot leak them."""
    needles = gate._SecretNeedles(("super-secret-token-abc123",))
    assert "super-secret-token-abc123" not in repr(needles)
    assert "super-secret-token-abc123" not in str(needles)
    assert "needles=1" in repr(needles)


def test_secret_scan_rejects_plaintext_tuple_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-18 security contract: the scan API must refuse a plaintext
    tuple — a repr-able tuple of secrets is exactly the leak the round-18 review
    forbade.  A TypeError surfaces at the boundary, never a silent accept."""
    _patch_root(monkeypatch, tmp_path)
    file = tmp_path / "evidence.json"
    file.write_text('{"k": "v"}\n', encoding="utf-8")
    with pytest.raises(TypeError, match="_SecretNeedles"):
        gate._secret_scan_paths([file], ("plaintext-secret",), "test")  # type: ignore[arg-type]


def test_secret_scan_rejects_plaintext_tuple_in_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 round-18: the staging wrapper carries the same type guard."""
    _patch_root(monkeypatch, tmp_path)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "evidence.json").write_text('{"k": "v"}\n', encoding="utf-8")
    with pytest.raises(TypeError, match="_SecretNeedles"):
        gate._secret_scan_staging(staging_dir, ("plaintext-secret",))  # type: ignore[arg-type]


def test_secret_needles_iterates_bytes() -> None:
    """C-122 round-18: the needle container yields encoded bytes ready for a
    substring scan, and deduplicates empty values."""
    needles = gate._SecretNeedles(("abc", "", "abc", "xyz"))
    assert list(needles) == [b"abc", b"xyz"]


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


def test_secret_scan_rejects_bare_credential_field_name() -> None:
    """C-122 round-18 gate-4 counter-example: a committed JSON artifact carrying
    a BARE credential field name (``token`` / ``cookie`` / ``secret`` /
    ``browser_token``) is a leak even when its value is already redacted or
    hash-shaped — the field name itself must never enter a Git object."""
    for field_name in ("token", "cookie", "secret", "browser_token"):
        data = json.dumps({"ok": True, field_name: "sk-abc"}).encode("utf-8")
        with pytest.raises(
            gate.GateStateChangedError, match="credential field name"
        ):
            gate._reject_credential_field_names(data, "evidence", "e.json")


def test_secret_scan_allows_nested_credential_like_structural_keys() -> None:
    """C-122 round-18 gate-4: a credential-shaped field name is only rejected on
    an EXACT bare match or the credential regex — a benign key that merely
    contains the letters (e.g. ``browser_tasks``, ``token_count``) is not a
    leak."""
    data = json.dumps(
        {"browser_tasks": [{"task_id": "t-1"}], "token_count": 3}
    ).encode("utf-8")
    gate._reject_credential_field_names(data, "evidence", "e.json")  # no raise


def test_secret_scan_rejects_unknown_64hex_under_scalar_key() -> None:
    """C-122 round-18 gate-4 counter-example: a bare 64-hex opaque value under a
    NON-digest key is indistinguishable from a bearer-token-shaped secret and must
    fail closed — only an explicit digest/binding key may hold a 64-hex value."""
    data = json.dumps({"provider": "ctrip", "value": "a" * 64}).encode("utf-8")
    with pytest.raises(
        gate.GateStateChangedError, match="unknown 64-hex value"
    ):
        gate._reject_unknown_64hex_values(data, "evidence", "e.json")


def test_secret_scan_allows_64hex_under_field_path_whitelist() -> None:
    """C-122 round-18 HG-F: a 64-hex value is a content-addressable binding ONLY
    at one of the exact committed field paths this gate itself produces
    (``files[].sha256``, companion ``build_sha256``, the layer-6 candidate /
    scenario / bridge / raw bindings) — every such path passes untouched."""
    allowed = [
        {"files": [{"name": "a.json", "sha256": "a" * 64}]},
        {
            "layer_verdicts": {
                "5_real_canary": {"companion": {"build_sha256": "a" * 64}}
            }
        },
        {"companion_status": {"companions": [{"build_sha256": "a" * 64}]}},
        {"api_payload_candidate_set_sha256": "a" * 64},
        {"scenario_sha256": "a" * 64},
        {
            "runtime_before_run": {
                "runtime_provenance": {"dependency_lock_sha256": "a" * 64}
            }
        },
        {
            "runtime_before_run": {
                "runtime_provenance": {"live_system_source_sha256": "a" * 64}
            }
        },
        {"bridge_state_lease_preflight": {"sha256": "a" * 64}},
        {"bridge_state_lease_postcheck": {"sha256": "a" * 64}},
        {"raw_evidence": {"sha256": "a" * 64}},
        {"done_gate": {"checks": [{"evidence": {"candidate_set_sha256": "a" * 64}}]}},
    ]
    for payload in allowed:
        data = json.dumps(payload).encode("utf-8")
        gate._reject_unknown_64hex_values(data, "evidence", "e.json")  # no raise


def test_secret_scan_allows_64hex_under_api_payload_sha256_path() -> None:
    """C-122 supervision 03:46 (Block 3): a real layer-6 compact carries the raw
    request payload's own SHA at the committed ``api_payload_sha256`` path — the
    secret scan must trust it (whitelisted), never reject a genuine publish as an
    unknown opaque 64-hex token."""
    data = json.dumps({"api_payload_sha256": "a" * 64}).encode("utf-8")
    gate._reject_unknown_64hex_values(data, "evidence", "e.json")  # no raise


def test_secret_scan_rejects_64hex_under_digest_named_unproduced_key() -> None:
    """C-122 round-18 HG-F counter-example: a key merely NAMED ``*_sha256`` /
    ``*_hash`` / ``*_digest`` / ``*_fingerprint`` is NOT enough — a 64-hex under
    a digest-named key that no committed artifact produces is still a leak."""
    for key in (
        "runtime_commit_sha",
        "asset_fingerprint",
        "record_hash",
        "build_digest",
        "evil_hash",
        "custom_fingerprint",
    ):
        data = json.dumps({key: "a" * 64}).encode("utf-8")
        with pytest.raises(
            gate.GateStateChangedError, match="unknown 64-hex value"
        ):
            gate._reject_unknown_64hex_values(data, "evidence", "e.json")


def test_secret_scan_rejects_64hex_under_whitelisted_key_at_wrong_path() -> None:
    """C-122 round-18 HG-F counter-example: the whitelist is PATH-scoped — a
    ``sha256`` / ``build_sha256`` key is only trusted at its produced committed
    path, so the same key nested under a foreign parent is rejected."""
    for payload in (
        {"opaque": {"sha256": "a" * 64}},
        {"companion_status": {"companions": [{"opaque": {"build_sha256": "a" * 64}}]}},
        {"done_gate": {"checks": [{"candidate_set_sha256": "a" * 64}]}},
    ):
        data = json.dumps(payload).encode("utf-8")
        with pytest.raises(
            gate.GateStateChangedError, match="unknown 64-hex value"
        ):
            gate._reject_unknown_64hex_values(data, "evidence", "e.json")


def test_secret_scan_rejects_unknown_64hex_nested_in_list() -> None:
    """C-122 round-18 gate-4 counter-example: the walker descends into lists too —
    a 64-hex under a non-digest key nested inside an array of objects is still a
    leak."""
    data = json.dumps(
        {"checks": [{"name": "c1", "opaque": "a" * 64}]}
    ).encode("utf-8")
    with pytest.raises(
        gate.GateStateChangedError, match="unknown 64-hex value"
    ):
        gate._reject_unknown_64hex_values(data, "evidence", "e.json")


def test_evidence_commits_use_fixed_non_personal_identity(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 gate-4: E and P are authored under the fixed non-personal
    identity (TripChord Done-Gate / done-gate@tripchord.invalid), never the
    ambient repo/user git config — so the published trail cannot be attributed to
    a human author and a hostile ambient config cannot author it either."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    p_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    for commit_sha in (evidence_commit, p_sha):
        info = subprocess.run(
            [
                "git",
                "-C",
                str(clean_repo),
                "show",
                "-s",
                "--format=%an%x00%ae%x00%cn%x00%ce",
                commit_sha,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip().split("\x00")
        assert info == [
            "TripChord Done-Gate",
            "done-gate@tripchord.invalid",
            "TripChord Done-Gate",
            "done-gate@tripchord.invalid",
        ]


def test_secret_scan_flags_short_alphanumeric_session_account(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 4 counter-example: a short alphanumeric session or account value
    in a query string must fail the gate.  Length and 4+ digit-number heuristics
    alone cannot see ``?session=abc123xyz`` or ``?user=alice123`` — the scan must
    be fail-closed by default (safe-key allowlist + hard session/account keys)."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    for url in (
        "https://flights.ctrip.com/online/list?session=abc123xyz",
        "https://accounts.ctrip.com/login?user=alice123",
        "https://book.qunar.com/pay?account=user_42",
    ):
        (staging_dir / "live-done-gate-v4.json").write_text(
            json.dumps({"result": {"source_urls": [url]}}), encoding="utf-8"
        )
        with pytest.raises(gate.GateStateChangedError, match="tracking URL"):
            gate.run_gate(staging_dir)


def test_secret_scan_flags_unknown_query_key_by_default(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 4 counter-example: a non-blank query value under a key that is
    neither on the safe allowlist nor a known session/account key is still a
    leak (default deny) — e.g. a bespoke ``?memberCode=alice123`` style param."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(
            {"result": {"source_urls": [
                "https://flights.ctrip.com/online/list?memberCode=alice123"
            ]}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="tracking URL"):
        gate.run_gate(staging_dir)


def test_secret_scan_flags_safe_key_with_opaque_value(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 4 counter-example: a safe allowlisted key is not a free pass —
    an opaque (long / non-short-code) value under ``?q=`` still leaks."""
    _patch_root(monkeypatch, clean_repo)
    _passing_layers(monkeypatch)
    _staging_evidence(staging_dir)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps(
            {"result": {"source_urls": [
                "https://flights.ctrip.com/online/list?q=SkJf9a2cBxW1qTzV4mNp8dQ"
            ]}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateStateChangedError, match="tracking URL"):
        gate.run_gate(staging_dir)


def test_commit_evidence_neutralizes_last_step_report_leak(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-118 Gap 3 counter-example: a secret that appears only in the report
    written at the very end of the evidence phase is NEUTRALIZED at dump
    redaction — the gate never commits, writes or prints the raw bytes, and the
    committed + delivered reports carry ``[REDACTED]`` instead.  This is
    strictly stronger than catching a leak after the fact: no exit path can
    leave the secret on disk or in stdout at all."""
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

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 0
    assert _porcelain(clean_repo) == ""
    # The committed report (in P) carries evidence_commit=E and the redacted
    # detail; the delivered staging report is equally clean.
    staging_text = (staging_dir / "product-v1-done-gate.json").read_text(
        encoding="utf-8"
    )
    assert token not in staging_text
    assert "[REDACTED]" in staging_text
    pointer_sha = _publish_ref(clean_repo, json.loads(staging_text)["run_id"])
    assert pointer_sha is not None
    committed_text = subprocess.run(
        ["git", "-C", str(clean_repo), "show", f"{pointer_sha}:{gate._REPORT_REL}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert token not in committed_text
    assert "[REDACTED]" in committed_text
    # HEAD stays at the tested revision — the leak-neutralized report lives only
    # on the side-channel ref.
    assert _head(clean_repo) != pointer_sha


# ---------------------------------------------------------------------------
# Two-phase evidence-commit atomicity (A4: atomic ref update, no intermediate E)
# ---------------------------------------------------------------------------


def _passing_report_and_start(clean_repo: Path) -> tuple[gate.GateReport, gate.GitSnapshot]:
    tested_sha = _head(clean_repo)
    report = gate.GateReport(
        schema_version=gate.EVIDENCE_SCHEMA,
        generated_at="2026-08-10T00:00:00+00:00",
        tested_commit_sha=tested_sha,
        run_id=_TEST_RUN_ID,
        toplevel=str(clean_repo),
        branch="main",
        worktree_dirty=False,
        layers=gate._passing_layers(),
        passed=True,
        summary="all applicable Done-Gate layers passed",
        boundary="",
    )
    return report, _expected_snapshot(clean_repo)


def _publish_ref(root: Path, run_id: str) -> str | None:
    """Resolve the side-channel gate ref, or None when it was never created."""
    ref = f"refs/tripchord/done-gate/{run_id}"
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", ref],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip()


def _assert_side_channel_published(
    root: Path,
    run_id: str,
    tested_sha: str,
    evidence_commit: str,
    expected_head: str | None = None,
) -> str:
    """C-122 P0: after a successful side-channel publish the product branch /
    HEAD / real index / worktree are byte-for-byte at S (or at a concurrent
    writer's commit, when ``expected_head`` is given) — the ONLY persistent
    change is the atomically-created gate ref -> P with P^=E and E^=S.  Returns
    the pointer-commit SHA P so callers can inspect P's committed tree."""
    if expected_head is None:
        assert _head(root) == tested_sha, "HEAD moved on a side-channel publish"
    else:
        assert _head(root) == expected_head, "concurrent HEAD not preserved"
    assert _porcelain(root) == "", "side-channel publish left a dirty tree"
    idx = subprocess.run(
        ["git", "-C", str(root), "diff-index", "--cached", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert idx.returncode == 0, "real index changed on a side-channel publish"
    p_sha = _publish_ref(root, run_id)
    assert p_sha is not None, f"gate ref refs/tripchord/done-gate/{run_id} missing"
    parent_p = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{p_sha}^"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert parent_p == evidence_commit, "P^ != E on a side-channel publish"
    parent_e = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{evidence_commit}^"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert parent_e == tested_sha, "E^ != S on a side-channel publish"
    # The product branch history is untouched — evidence commits are never
    # reachable from HEAD (side-channel only, never the branch tip).
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Done-Gate evidence" not in log
    assert "evidence_commit=" not in log
    # No tracked report/evidence was ever written into the shared worktree.
    assert not (
        root / "benchmarks" / "results" / "product-v1-done-gate.json"
    ).exists()
    return p_sha


def _assert_phase_failure_is_atomic(
    clean_repo: Path,
    staging_dir: Path,
    tested_sha: str,
    run_id: str = _TEST_RUN_ID,
) -> None:
    """After any phase-1/phase-2/add/update-ref failure the branch must still be
    at the tested revision, the object graph may not expose an intermediate E on
    the branch, the index + worktree must be byte-for-byte clean, and the
    side-channel gate ref must NOT have been created (nothing was published)."""
    assert _head(clean_repo) == tested_sha, "branch moved on a failed commit phase"
    assert _porcelain(clean_repo) == "", "failed commit phase left a dirty tree"
    idx = subprocess.run(
        ["git", "-C", str(clean_repo), "diff-index", "--cached", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert idx.returncode == 0, "real index changed on a failed commit phase"
    # No intermediate commit may be reachable from the branch tip.
    log = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--oneline", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Done-Gate evidence" not in log
    assert "evidence_commit=" not in log
    # The side-channel ref was never created — a failed phase publishes nothing.
    assert _publish_ref(clean_repo, run_id) is None, (
        "gate ref created on a failed commit phase"
    )


def test_commit_evidence_phase1_add_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed phase-1 temp-index ``update-index`` (the
    side-channel equivalent of ``git add``) leaves the branch on the tested
    revision, no intermediate commit, no gate ref, and a clean index/worktree."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "update-index", when=1)

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_phase1_commit_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """A4 counter-example: a failed phase-1 ``commit-tree`` leaves the branch on
    the tested revision — E is never installed as HEAD."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
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
    _populate_full_required_evidence(monkeypatch, staging_dir)
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
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    _inject_git_failure(monkeypatch, "update-ref")

    with pytest.raises(gate.GateStateChangedError):
        gate._commit_evidence(staging_dir, report, start=start)
    _assert_phase_failure_is_atomic(clean_repo, staging_dir, start.commit_sha)


def test_commit_evidence_success_creates_ref_once_atomically(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 positive control: on success HEAD does NOT move — the product
    branch stays at the tested revision S, the real index stays in sync with it,
    the worktree stays clean, and the ONLY atomic persistent change is the
    create-only gate ref ``refs/tripchord/done-gate/<run_id>`` -> P with
    P^=E and E^=S."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, start.commit_sha, evidence_commit
    )
    # P's committed authoritative report records evidence_commit=E, passed=true
    # and the gate-ref binding.
    committed = json.loads(
        subprocess.run(
            ["git", "-C", str(clean_repo), "show", f"{pointer_sha}:{gate._REPORT_REL}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert committed["evidence_commit"] == evidence_commit
    assert committed["passed"] is True
    assert committed["gate_ref"] == f"refs/tripchord/done-gate/{_TEST_RUN_ID}"


def test_commit_evidence_no_fallible_op_after_cas(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-114 + C-122 P0 counter-example (CAS 成功后检查异常): after the atomic
    create-only ``update-ref`` of the gate ref succeeds there must be NO fallible
    operation left.  The side-channel publish's update-ref is literally the last
    ``_git`` invocation — nothing (snapshot, verify, scan, restore, re-dump) runs
    after it, so a failure there can never flip a published gate into a
    voided-but-published state."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    real_git = gate._git
    calls: list[tuple[str, ...]] = []

    def recording_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return real_git(*args, **kwargs)

    monkeypatch.setattr(gate, "_git", recording_git)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, start.commit_sha, evidence_commit
    )

    # The publish update-ref ran exactly once, as the LAST git invocation, and
    # used ``--no-deref`` so a symref racing in cannot push P into a victim ref
    # (C-122 P0 symbolic-ref hijack guard).
    ref_calls = [c for c in calls if c and c[0] == "update-ref"]
    assert len(ref_calls) == 1
    assert ref_calls[0][1:] == (
        "--no-deref",
        f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
        pointer_sha,
        "0" * 40,
    )
    assert calls[-1] == ref_calls[0], "a git call ran AFTER the publish update-ref"
    # The gate succeeded with the product branch untouched.


def test_commit_evidence_construction_crash_at_each_stage_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: a process crashing at ANY construction stage —
    phase 1 staging, phase 2 (after E is materialized), between P and the
    publish, or at the publish itself — leaves the shared repo byte-for-byte at
    S.  The shared worktree is never written and the gate ref is only created by
    the final atomic update-ref, so a crash before that point leaves at most
    unreachable objects and no visible repo change.  An unexpected exception
    (RuntimeError, the way a real crash surfaces) escapes the commit phase."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)

    crash_points = {
        "phase1_stage": "_verify_evidence_file_safety",
        "phase2_after_e": "_verify_evidence_contract",
        "after_p_before_publish": "_final_evidence_secret_scan",
        "at_publish": "update-ref",
    }
    for label, target in crash_points.items():
        report, start = _passing_report_and_start(clean_repo)
        tested_sha = start.commit_sha

        # Each crash point is patched in an isolated context so a crash from a
        # previous iteration can never leak into this one.
        with monkeypatch.context() as isolated:
            if target == "update-ref":
                real_git = gate._git

                def crashing_git(
                    *args: str,
                    _real_git=real_git,
                    _label: str = label,
                    **kwargs: object,
                ) -> subprocess.CompletedProcess:
                    if args and args[0] == "update-ref":
                        raise RuntimeError(f"simulated crash at {_label}")
                    return _real_git(*args, **kwargs)

                isolated.setattr(gate, "_git", crashing_git)
            else:

                def crash(
                    *args: object, _label: str = label, **kwargs: object
                ) -> object:
                    raise RuntimeError(f"simulated crash at {_label}")

                isolated.setattr(gate, target, crash)

            with pytest.raises(RuntimeError, match=f"simulated crash at {label}"):
                gate._commit_evidence(staging_dir, report, start=start)

        assert _head(clean_repo) == tested_sha, f"HEAD moved on crash at {label}"
        assert _porcelain(clean_repo) == "", f"dirty tree on crash at {label}"
        idx = subprocess.run(
            ["git", "-C", str(clean_repo), "diff-index", "--cached", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
        )
        assert idx.returncode == 0, f"real index changed on crash at {label}"
        assert _publish_ref(clean_repo, _TEST_RUN_ID) is None, (
            f"gate ref created on crash at {label}"
        )
        assert not (
            clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
        ).exists(), f"tracked report written on crash at {label}"


def test_commit_evidence_same_sha_other_branch_switch_leaves_both_untouched(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: a concurrent checkout of a DIFFERENT branch at
    the SAME SHA must NOT disturb the side-channel publish.  The gate never
    updates HEAD or the branch refs — only the namespaced gate ref — so HEAD
    stays on 'other' after the concurrent switch, BOTH branches still sit at S,
    and the evidence trail is published through the dedicated ref with P^=E,
    E^=S."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    tested_sha = start.commit_sha

    # A sibling branch at the SAME commit, with HEAD on it.
    subprocess.run(
        ["git", "-C", str(clean_repo), "branch", "other", "main"], check=True
    )

    real_final_scan = gate._final_evidence_secret_scan

    def checkout_other_after_final_scan(staging: Path, tracked: object) -> None:
        # The real final scan runs first, then a real concurrent ``git
        # symbolic-ref`` switches HEAD to another branch at the same SHA before
        # the gate publishes.  ``git symbolic-ref`` is a ref-only op, so it
        # succeeds and the gate never rewrites it.
        real_final_scan(staging, tracked)  # type: ignore[arg-type]
        subprocess.run(
            ["git", "-C", str(clean_repo), "symbolic-ref", "HEAD", "refs/heads/other"],
            check=True,
        )

    monkeypatch.setattr(
        gate, "_final_evidence_secret_scan", checkout_other_after_final_scan
    )

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The publish succeeded: HEAD is on 'other' (the concurrent switch survived
    # untouched), BOTH branches sit at S, the worktree is clean, and the gate
    # ref carries the evidence trail.
    sym = subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert sym == "other"
    assert _head(clean_repo) == tested_sha
    for ref in ("refs/heads/main", "refs/heads/other"):
        tip = subprocess.run(
            ["git", "-C", str(clean_repo), "rev-parse", ref],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert tip == tested_sha, f"{ref} moved on a side-channel publish"
    assert _porcelain(clean_repo) == ""
    _assert_side_channel_published(clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit)


def test_commit_evidence_concurrent_commit_parent_tree_index_preserved(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: a REAL concurrent commit racing the gate's
    commit phase is completely unaffected AND the side-channel publish still
    succeeds.  The concurrent writer commits on top of the tested revision; the
    gate never touches HEAD/branch/real index/worktree, so the concurrent
    commit's parent, tree and index state all survive byte-for-byte, and the
    evidence trail lands on the dedicated gate ref (P^=E, E^=S) instead.

    The monkeypatch only runs a real ``git add``/``git commit`` at the boundary
    after the gate's final secret scan; the commit itself and the resulting
    ref/index state are all real, unmonkeypatched git behavior."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    tested_sha = start.commit_sha

    concurrent_sha: list[str | None] = [None]

    real_final_scan = gate._final_evidence_secret_scan

    def concurrent_commit_after_final_scan(
        staging: Path, tracked_paths: object
    ) -> None:
        # Run the gate's real final scan first, then a real concurrent writer
        # commits on top of the tested revision.
        real_final_scan(staging, tracked_paths)  # type: ignore[arg-type]
        (clean_repo / "concurrent.txt").write_text(
            "concurrent work\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(clean_repo), "add", "concurrent.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(clean_repo), "commit", "-q", "-m", "concurrent"],
            check=True,
        )
        concurrent_sha[0] = _head(clean_repo)

    monkeypatch.setattr(
        gate, "_final_evidence_secret_scan", concurrent_commit_after_final_scan
    )

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    assert concurrent_sha[0] is not None
    # 1. The branch sits on the concurrent commit, never on P/E/S.
    assert _head(clean_repo) == concurrent_sha[0]
    # 2. The concurrent commit's parent is the tested revision S — it was not
    #    rebased or absorbed.
    parent = subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", f"{concurrent_sha[0]}^"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert parent == tested_sha
    # 3. The concurrent commit's tree carries exactly its own file — the gate's
    #    staged evidence never leaked into it.
    tree_files = subprocess.run(
        ["git", "-C", str(clean_repo), "ls-tree", "--name-only", concurrent_sha[0]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "concurrent.txt" in tree_files
    assert not any("benchmarks/results" in f for f in tree_files)
    # 4. The real index matches the concurrent commit's tree — the gate never
    #    clobbered the concurrent index state.
    idx = subprocess.run(
        ["git", "-C", str(clean_repo), "diff-index", "--cached", "--quiet",
         concurrent_sha[0]],
        capture_output=True,
        text=True,
    )
    assert idx.returncode == 0
    # 5. The worktree is clean and the gate's own evidence files were never
    #    written into the shared tree; only the concurrent commit's file
    #    remains, committed.
    assert _porcelain(clean_repo) == ""
    assert not (
        clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    ).exists()
    # 6. No evidence commit is reachable from the concurrent tip — the trail is
    #    only on the dedicated side-channel ref.
    log = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Done-Gate evidence" not in log
    assert concurrent_sha[0] is not None
    _assert_side_channel_published(
        clean_repo,
        _TEST_RUN_ID,
        tested_sha,
        evidence_commit,
        expected_head=concurrent_sha[0],
    )


def test_commit_evidence_leaves_concurrent_staged_index_untouched(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 counter-example: when a concurrent writer has STAGED (not
    committed) work in the real index, the side-channel publish must NOT clobber
    that staged state — the gate never reads or writes the real index.  The
    staged file survives byte-for-byte, the branch never moves, and only the
    dedicated gate ref appears."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    tested_sha = start.commit_sha

    # A concurrent writer stages a file in the real index.
    (clean_repo / "staged.txt").write_text("staged work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clean_repo), "add", "staged.txt"], check=True)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    # The publish succeeded: the branch never moved, and the concurrent staged
    # work is still staged exactly as the writer left it.
    assert _head(clean_repo) == tested_sha
    staged = subprocess.run(
        ["git", "-C", str(clean_repo), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert staged == "staged.txt"
    # The gate's own report/evidence were never written into the shared tree;
    # only the writer's staged file remains (as an index entry, never
    # committed).
    assert not (
        clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    ).exists()
    # The side-channel trail was published (P^=E, E^=S) without touching the
    # index; HEAD stays at S.
    p_sha = _publish_ref(clean_repo, _TEST_RUN_ID)
    assert p_sha is not None
    parent_p = subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", f"{p_sha}^"],
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
    assert parent_e == tested_sha


def test_commit_evidence_success_leaves_head_index_worktree_consistent(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 positive control: on a successful side-channel publish the
    HEAD/index/worktree must all agree AND stay at the tested revision S — the
    gate never advances HEAD to P/E, never writes the real index, and never
    touches the worktree, so ``diff-index --cached`` against HEAD is quiet and
    no index sync exists to race at all.  The evidence trail lives only on the
    dedicated gate ref."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)

    head = _head(clean_repo)
    # 1. HEAD stays exactly at the tested revision S — never P/E.
    assert head == start.commit_sha
    # 2. The real index matches HEAD's tree (HEAD/index consistent, untouched).
    idx = subprocess.run(
        ["git", "-C", str(clean_repo), "diff-index", "--cached", "--quiet", head],
        capture_output=True,
        text=True,
    )
    assert idx.returncode == 0
    # 3. The worktree is clean too (HEAD/worktree consistent, untouched).
    assert _porcelain(clean_repo) == ""
    # 4. The dedicated gate ref carries the atomic trail S -> E -> P.
    _assert_side_channel_published(clean_repo, _TEST_RUN_ID, start.commit_sha, evidence_commit)


def _pointer_commit(clean_repo: Path, parent_sha: str) -> str:
    """A real child commit (same tree as ``parent_sha``) usable as a pointer P."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "commit-tree",
            f"{parent_sha}^{{tree}}",
            "-p",
            parent_sha,
            "-m",
            "P",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_publish_gate_ref_rejects_preset_direct_ref(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 P0 symref-hijack guard: a PRE-SEEDED direct gate ref is a conflict —
    ``_publish_gate_ref`` refuses the create-only publish into it and leaves the
    ref, HEAD, index and worktree byte-for-byte untouched."""
    _patch_root(monkeypatch, clean_repo)
    gate_ref = f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    tested_sha = _head(clean_repo)
    pointer = _pointer_commit(clean_repo, tested_sha)
    subprocess.run(
        ["git", "-C", str(clean_repo), "update-ref", gate_ref, tested_sha], check=True
    )
    with pytest.raises(gate.GateStateChangedError, match="preset as direct"):
        gate._publish_gate_ref(gate_ref, pointer, {})
    # The preset ref is unchanged (never overwritten by P).
    assert subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", gate_ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == tested_sha
    # Product branch / HEAD / worktree all unchanged.
    assert _head(clean_repo) == tested_sha
    assert _porcelain(clean_repo) == ""


def test_publish_gate_ref_rejects_preset_symref_leaves_victim_untouched(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 P0 symref-hijack counter-example: with the gate ref PRE-SEEDED as a
    symbolic ref -> victim (a victim ref that does NOT exist yet — the state in
    which a dereferencing ``update-ref`` would CREATE the victim holding P), the
    publish must FAIL.  The passing evidence must never land at the victim name,
    and the symref itself must not be silently converted to a direct ref."""
    _patch_root(monkeypatch, clean_repo)
    gate_ref = f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    tested_sha = _head(clean_repo)
    pointer = _pointer_commit(clean_repo, tested_sha)
    subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", gate_ref, "refs/heads/victim"],
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="symbolic-ref hijack"):
        gate._publish_gate_ref(gate_ref, pointer, {})
    # 1. The victim was never created.
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/heads/victim",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "victim ref was created by the publish"
    # 2. The gate ref is still the SAME symref (not silently converted to P).
    sym = subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", gate_ref],
        capture_output=True,
        text=True,
    )
    assert sym.returncode == 0
    assert sym.stdout.strip() == "refs/heads/victim"
    # 3. Product branch / HEAD / worktree all unchanged.
    assert _head(clean_repo) == tested_sha
    assert _porcelain(clean_repo) == ""


def test_publish_gate_ref_receipt_lost_but_publish_landed(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 round-18 gate-3 terminal state: the ``update-ref`` receipt is LOST
    (a timeout/caller-side error surfaces after the update landed), but the
    read-only reconciliation sees the ref holding exactly P — the publish
    counts as success and returns normally, never a spurious fail."""
    _patch_root(monkeypatch, clean_repo)
    gate_ref = f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    tested_sha = _head(clean_repo)
    pointer = _pointer_commit(clean_repo, tested_sha)
    real_git = gate._git

    def _lost_receipt(*args: object, **kwargs: object) -> object:
        if args and args[0] == "update-ref":
            # Simulate the update LANDING but the receipt being lost: run the
            # real update-ref, then raise as if the call timed out.
            real_git(*args, **kwargs)
            raise gate.GateStateChangedError("update-ref timed out")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(gate, "_git", _lost_receipt)
    gate._publish_gate_ref(gate_ref, pointer, {})  # must NOT raise
    # The reconciliation accepted the landed publish: ref == P.
    assert subprocess.run(
        ["git", "-C", str(clean_repo), "rev-parse", gate_ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == pointer


def test_publish_gate_ref_reconciliation_read_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 round-18 gate-3 terminal state: the ``update-ref`` fails AND the
    read-only reconciliation probe itself fails (git unreadable) — the outcome
    is genuinely unknowable, so the publish fails closed instead of guessing."""
    _patch_root(monkeypatch, clean_repo)
    gate_ref = f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    tested_sha = _head(clean_repo)
    pointer = _pointer_commit(clean_repo, tested_sha)

    def _failing_publish(*args: object, **kwargs: object) -> object:
        if args and args[0] == "update-ref":
            raise gate.GateStateChangedError("update-ref failed")
        raise gate.GateStateChangedError("git is unavailable")  # probe also fails

    monkeypatch.setattr(gate, "_git", _failing_publish)
    with pytest.raises(gate.GateStateChangedError, match="git is unavailable"):
        gate._publish_gate_ref(gate_ref, pointer, {})


def test_commit_evidence_symref_hijack_fails_closed_and_leaves_everything_untouched(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 REAL temp-repo counter-example through the FULL commit path: with
    the gate ref pre-seeded as a symref -> victim, ``_commit_evidence`` runs the
    entire staging/commit phase but the create-only publish FAILS CLOSED.  The
    victim ref and the gate ref are never rewritten, the product branch / HEAD /
    real index / worktree stay byte-for-byte at S, and the failed report carries
    no evidence_commit / gate_ref claim."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    gate_ref = f"refs/tripchord/done-gate/{_TEST_RUN_ID}"
    subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", gate_ref, "refs/heads/victim"],
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="symbolic-ref hijack"):
        gate._commit_evidence(staging_dir, report, start=start)
    # 1. The victim was never created (a dereferencing publish would have
    #    materialised it holding P).
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/heads/victim",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "victim ref was created by the evidence commit"
    # 2. The gate ref is still the same symref — untouched, not converted.
    sym = subprocess.run(
        ["git", "-C", str(clean_repo), "symbolic-ref", gate_ref],
        capture_output=True,
        text=True,
    )
    assert sym.returncode == 0
    assert sym.stdout.strip() == "refs/heads/victim"
    # 3. HEAD stays at S and the worktree stays clean.
    assert _head(clean_repo) == tested_sha
    assert _porcelain(clean_repo) == ""
    # 4. The real index still matches HEAD (never read-tree'd/clobbered).
    idx = subprocess.run(
        ["git", "-C", str(clean_repo), "diff-index", "--cached", "--quiet", tested_sha],
        capture_output=True,
        text=True,
    )
    assert idx.returncode == 0
    # 5. The failed report claims no evidence_commit / gate_ref.
    assert report.evidence_commit is None
    assert report.gate_ref is None


def test_verify_gate_ref_rejects_symref_hijack(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 P0 symref-hijack guard: a gate ref that has been turned into a
    symbolic ref must fail verification closed — a dereferenced read would hand
    back the victim's OID and masquerade as a published trail."""
    report, start, tested_sha = _minimal_evidence_commit_args(
        monkeypatch, clean_repo, staging_dir
    )
    evidence_commit = gate._commit_evidence(staging_dir, report, start=start)
    _pointer_sha = _assert_side_channel_published(
        clean_repo, _TEST_RUN_ID, tested_sha, evidence_commit
    )
    # Hijack: convert the published direct ref into a symref -> main.
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "symbolic-ref",
            f"refs/tripchord/done-gate/{_TEST_RUN_ID}",
            "refs/heads/main",
        ],
        check=True,
    )
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False
    assert any("symbolic ref" in problem for problem in verdict["problems"])
    # The dereferenced OID happens to be a real commit, but that must NOT pass.
    assert "pointer_commit" not in verdict


def test_latest_gate_run_id_skips_symref_hijack(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path
) -> None:
    """C-122 P0 symref-hijack guard: ``--latest`` resolution must never resolve
    to a symbolic ref under the gate namespace — with only a hijack symref
    present it raises instead of pretending a run was published."""
    _patch_root(monkeypatch, clean_repo)
    subprocess.run(
        [
            "git",
            "-C",
            str(clean_repo),
            "symbolic-ref",
            "refs/tripchord/done-gate/999999999999",
            "refs/heads/main",
        ],
        check=True,
    )
    with pytest.raises(gate.GateStateChangedError, match="no published gate refs"):
        gate._latest_gate_run_id()


def test_commit_evidence_rejects_tampered_e_manifest(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 (10:00 review #2) counter-example: a TAMPERED E manifest —
    a committed-file hash altered before phase 1 — must fail the phase closed
    when E's manifest is re-parsed from the committed blob.  The branch never
    moves and the index/worktree stay clean."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)

    real_write_manifest = gate._write_manifest
    phase = [0]

    def tampered_write(manifest: dict[str, Any], target: Path) -> Path:
        phase[0] += 1
        if phase[0] == 1:
            # Phase-1 manifest: corrupt the first committed file's sha256 so E's
            # committed manifest can no longer match the committed bytes.
            manifest = dict(manifest)
            files = list(manifest.get("files") or [])
            for index, entry in enumerate(files):
                if entry.get("committed") is True:
                    files[index] = dict(entry)
                    files[index]["sha256"] = "0" * 64
                    break
            manifest["files"] = files
        return real_write_manifest(manifest, target)

    monkeypatch.setattr(gate, "_write_manifest", tampered_write)

    with pytest.raises(gate.GateStateChangedError, match="sha256"):
        gate._commit_evidence(staging_dir, report, start=start)

    # Fail closed: the branch never moved, and no evidence commit is reachable.
    assert _head(clean_repo) == start.commit_sha
    assert _porcelain(clean_repo) == ""
    log = subprocess.run(
        ["git", "-C", str(clean_repo), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Done-Gate evidence" not in log


def test_commit_evidence_rejects_non_json_pointer_report(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 (10:00 review #2) counter-example: P's authoritative report
    must be parsed from the committed blob and be valid JSON with the full
    schema.  A non-JSON P report (tampered at the phase-2 dump) fails closed
    before the CAS — the branch never moves."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    staged_report_path = staging_dir / gate._REPORT_STAGED_NAME

    real_dump = gate._dump

    def tampered_p_dump(report_obj: gate.GateReport, output_path: Path) -> Path:
        if (
            report_obj.evidence_commit is not None
            and output_path == staged_report_path
        ):
            # Phase-2 staged report: poison it so P's committed blob is not JSON.
            gate._write_atomic(output_path, "{not valid json", 0o600)
            return output_path
        return real_dump(report_obj, output_path)

    monkeypatch.setattr(gate, "_dump", tampered_p_dump)

    # C-122 supervision 07:29 (gap 2): the malformed top-level JSON check now
    # fires FIRST in the final secret scan (the poisoned ``{not valid json``
    # staged report is a ``.json`` evidence artifact), so the message names the
    # malformed top-level JSON rather than the later committed-blob parse.  The
    # fail-closed outcome is identical: the branch never moves.
    with pytest.raises(gate.GateStateChangedError, match="malformed top-level JSON"):
        gate._commit_evidence(staging_dir, report, start=start)

    # Fail closed: the branch never moved, the tree is clean.
    assert _head(clean_repo) == start.commit_sha
    assert _porcelain(clean_repo) == ""


def test_commit_evidence_fails_closed_when_whitelist_bytes_change_after_final_scan(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 round-18 (10:00 review #2) counter-example: a whitelist path's bytes
    changed AFTER the final secret scan (the TOCTOU between scan and CAS) must
    fail the phase closed — the gate refuses to commit a tree that differs from
    what it scanned, instead of silently committing different bytes."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    report, start = _passing_report_and_start(clean_repo)
    staged_report_path = staging_dir / gate._REPORT_STAGED_NAME

    real_final_scan = gate._final_evidence_secret_scan

    def scan_then_mutate(staging: Path, tracked_paths: object) -> None:
        real_final_scan(staging, tracked_paths)  # type: ignore[arg-type]
        # A concurrent writer mutates the staged report right after the scan,
        # before the gate's publish — the bytes P committed (from the pre-scan
        # file) no longer match the on-disk staging file.
        with staged_report_path.open("a", encoding="utf-8") as handle:
            handle.write("\n// tampered after final scan\n")

    monkeypatch.setattr(gate, "_final_evidence_secret_scan", scan_then_mutate)

    with pytest.raises(
        gate.GateStateChangedError, match="bytes differ from the scanned staging file"
    ):
        gate._commit_evidence(staging_dir, report, start=start)

    # Fail closed: the branch never moved, the shared worktree is untouched, and
    # no gate ref was published.
    assert _head(clean_repo) == start.commit_sha
    assert _porcelain(clean_repo) == ""
    assert _publish_ref(clean_repo, _TEST_RUN_ID) is None


# ---------------------------------------------------------------------------
# C-118 eight hard-gap counter-examples (each paired with a real failing
# reproduction in review C-116 / the supervisor pass)
# ---------------------------------------------------------------------------


def test_main_no_post_cas_dump_local_report_carries_evidence_commit(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 7 + P0 counter-example: the delivered report is finalized BEFORE
    the publish — there is no post-CAS re-dump to swallow.  Exactly three dumps
    run (the pre-commit staging report, the phase-1 E report and the phase-2 P
    report; the delivered local report IS the phase-2 staged file), and the
    local report on disk carries evidence_commit=E and the gate-ref binding
    matching the committed report.  HEAD stays at S — only the gate ref
    appears."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    head_before = _head(clean_repo)

    real_dump = gate._dump
    dump_count = [0]

    def counting_dump(report: gate.GateReport, output_path: Path | None = None) -> Path:
        dump_count[0] += 1
        return real_dump(report, output_path)

    monkeypatch.setattr(gate, "_dump", counting_dump)

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 0
    assert dump_count[0] == 3
    assert _porcelain(clean_repo) == ""
    assert _head(clean_repo) == head_before
    # No tracked report was ever written into the shared worktree.
    assert not (
        clean_repo / "benchmarks" / "results" / "product-v1-done-gate.json"
    ).exists()
    local = json.loads(
        (staging_dir / "product-v1-done-gate.json").read_text(encoding="utf-8")
    )
    assert local["passed"] is True
    assert local["evidence_commit"]
    assert local["gate_ref"] == f"refs/tripchord/done-gate/{local['run_id']}"
    # The published pointer commit's report agrees with the delivered local one.
    pointer_sha = _publish_ref(clean_repo, local["run_id"])
    assert pointer_sha is not None
    committed = json.loads(
        subprocess.run(
            ["git", "-C", str(clean_repo), "show", f"{pointer_sha}:{gate._REPORT_REL}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert committed["evidence_commit"] == local["evidence_commit"]
    assert committed["passed"] is True


def _gap3_manifest_rename_entry(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: rename a fixed evidence entry to a foreign name —
    the name set no longer matches the fixed contract."""
    for entry in manifest["files"]:  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("name") == "browser-e2e.json":
            entry["name"] = "browser-e2e-renamed.json"
            return manifest
    return manifest


def _gap3_manifest_drop_entry(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: drop a fixed evidence entry from the file set."""
    manifest["files"] = [  # type: ignore[index]
        entry
        for entry in manifest["files"]  # type: ignore[index]
        if not (isinstance(entry, dict) and entry.get("name") == "browser-e2e.json")
    ]
    return manifest


def _gap3_manifest_smuggle_entry(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: smuggle a foreign evidence entry into the set."""
    manifest["files"].append(  # type: ignore[index]
        {
            "name": "smuggled-evidence.json",
            "tracked_path": "benchmarks/results/smuggled-evidence.json",
            "sha256": "a" * 64,
            "size_bytes": 0,
            "committed": True,
        }
    )
    return manifest


def _gap3_manifest_relocate_entry(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: relocate a compact's tracked_path off the canonical
    contract path — the name set and every field shape still read correctly."""
    for entry in manifest["files"]:  # type: ignore[index]
        if (
            isinstance(entry, dict)
            and entry.get("name") == gate._COMPACT_CANARY_STAGED_NAME
        ):
            entry["tracked_path"] = "benchmarks/results/moved-layer5-compact.json"
            return manifest
    return manifest


def _gap3_manifest_flip_committed(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: flip a fixed committed artifact's committed flag to
    false (hash-only raw) — no fixed contract permits a committed artifact to
    masquerade as a git-ignored original."""
    for entry in manifest["files"]:  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("name") == "product-acceptance.json":
            entry["committed"] = False
            return manifest
    return manifest


def _gap3_manifest_bad_field_set(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: a file entry with an unexpected extra field."""
    for entry in manifest["files"]:  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("name") == "product-acceptance.json":
            entry["smuggled_field"] = True
            return manifest
    return manifest


def _gap3_manifest_duplicate_name(
    manifest: dict[str, object],
) -> dict[str, object]:
    """Gap-3 counter-example: a duplicate evidence file name in the set."""
    files = manifest["files"]  # type: ignore[index]
    if files:
        files.append(dict(files[0]))
    return manifest


@pytest.mark.parametrize(
    "label,mutate,needle",
    [
        (
            "renamed evidence file",
            _gap3_manifest_rename_entry,
            "not a fixed evidence-contract name",
        ),
        ("missing evidence file", _gap3_manifest_drop_entry, "file-name set"),
        (
            "smuggled extra evidence file",
            _gap3_manifest_smuggle_entry,
            "not a fixed evidence-contract name",
        ),
        ("relocated tracked_path", _gap3_manifest_relocate_entry, "relocated"),
        (
            "flipped committed flag",
            _gap3_manifest_flip_committed,
            "committed flag flipped",
        ),
        (
            "unexpected field set",
            _gap3_manifest_bad_field_set,
            "unexpected field set",
        ),
        ("duplicate file name", _gap3_manifest_duplicate_name, "repeats file name"),
    ],
)
def test_gap3_manifest_contract_publish_and_resolver_reject_identically(
    label: str,
    mutate: Callable[[dict[str, object]], dict[str, object]],
    needle: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    tmp_path: Path,
) -> None:
    """C-122 round-19 02:56 supervision (gap 3): the SINGLE canonical manifest
    validator means the publish preflight (``_verify_evidence_contract``) and the
    consumer resolver (``verify_gate_ref``) reject the SAME structural violation
    with the SAME message — a renamed / missing / smuggled evidence entry, a
    relocated ``tracked_path``, a flipped ``committed`` flag, an unexpected field
    set, or a duplicate name can never be published on one side and accepted on
    the other."""
    e2, _p2 = _forge_repointed_chain(
        monkeypatch,
        clean_repo,
        staging_dir,
        tmp_path,
        mutate_e_manifest=mutate,
    )
    # Resolver-side: the consumer rejects the forged E/P chain.
    verdict = gate.verify_gate_ref(_TEST_RUN_ID)
    assert verdict["verified"] is False, f"{label}: resolver accepted the forgery"
    assert any(
        needle in problem for problem in verdict["problems"]
    ), f"{label}: resolver problems {verdict['problems']!r} missing {needle!r}"
    # Publish-side: the producer's post-commit preflight rejects the SAME forged
    # E commit with the SAME structural violation.
    tested_sha = _head(clean_repo)
    with pytest.raises(gate.GateStateChangedError) as exc_info:
        gate._verify_evidence_contract(
            e2, staging_dir, tested_commit_sha=tested_sha, run_id=_TEST_RUN_ID
        )
    assert needle in str(exc_info.value), (
        f"{label}: publish-side raised {exc_info.value!r} missing {needle!r}"
    )


def test_main_local_report_dump_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 Fix 7 counter-example: if the delivered local report cannot be
    dumped inside the commit phase, the whole gate fails exit 2 and HEAD never
    moves — the local report is generated pre-CAS and is not a silent
    best-effort step."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    head_before = _head(clean_repo)

    real_dump = gate._dump
    dump_count = [0]

    def flaky_dump(report: gate.GateReport, output_path: Path | None = None) -> Path:
        dump_count[0] += 1
        if dump_count[0] == 3:  # the delivered local report dump in _commit_evidence
            raise OSError("local report dump failed")
        return real_dump(report, output_path)

    monkeypatch.setattr(gate, "_dump", flaky_dump)

    rc = gate.main(["--staging-dir", str(staging_dir), "--commit-evidence", "--quiet"])

    assert rc == 2
    assert _head(clean_repo) == head_before


def test_layer5_fails_when_canary_exits_nonzero_despite_green_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-118 Gap 2 counter-example: a canary process that exits non-zero while
    leaving an all-green certified JSON must fail the layer — the process exit
    code is part of the canary contract and a forged JSON cannot paper over it."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (1, ""))
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(_matching_canary()), encoding="utf-8"
    )
    result = gate.layer5_real_canary(staging_dir)
    assert result.passed is False
    assert any("exit" in (check.get("detail") or "") for check in result.sub_checks)


def test_layer5_rejects_extra_scopes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-118 Gap 2 counter-example: a canary that adds a non-certified extra
    scope must fail the layer — coverage is the exact registry-derived
    certified canary scopes, and an ad-hoc scope inflates it."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setattr(gate, "_run", lambda cmd, **kwargs: (0, ""))
    canary = _matching_canary()
    canary["scopes"].append(  # type: ignore[union-attr]
        {
            "scope": "ctrip:car",
            "kind": "companion_heartbeat",
            "passed": True,
            "fresh": True,
            "authorized": True,
            "read_only": True,
        }
    )
    (staging_dir / "live-canary-certified.json").write_text(
        json.dumps(canary), encoding="utf-8"
    )
    result = gate.layer5_real_canary(staging_dir)
    assert result.passed is False
    assert any("extra" in (check.get("detail") or "") for check in result.sub_checks)


def test_main_failed_gate_report_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C-118 Gap 3 counter-example: a failed gate whose layer detail carries a
    secret must never write or print the raw bytes — the on-disk report and the
    printed output both carry ``[REDACTED]`` instead.

    Redaction runs at dump (in-place on the report) so every later consumer —
    the secret scan, the failed-gate print, the on-disk JSON — only ever sees
    neutralized bytes."""
    _patch_root(monkeypatch, clean_repo)
    _populating_passing_layers(monkeypatch, staging_dir)
    token = "F" * 64
    monkeypatch.setattr(gate, "_bridge_token", lambda: token)
    monkeypatch.setattr(
        gate,
        "layer6_full_e2e",
        lambda *args, **kwargs: gate.LayerResult(
            name="6_full_e2e", passed=False, detail=f"secret={token}"
        ),
    )

    rc = gate.main(["--staging-dir", str(staging_dir)])

    assert rc == 2
    report_text = (staging_dir / "product-v1-done-gate.json").read_text(
        encoding="utf-8"
    )
    assert token not in report_text
    assert "[REDACTED]" in report_text
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert "[REDACTED]" in captured.out


def test_bridge_state_lease_preflight_detects_queued_work(tmp_path: Path) -> None:
    """C-118 Gap 4 counter-example: the bridge keeps queued/claimed task and
    pending reorder state outside the planning ``jobs`` table, so the layer-6
    residual-lease preflight must read the persisted bridge-state JSON — not
    only ``tripchord.db``."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [
                    {"id": "task-1", "state": "queued"},
                    {"id": "task-2", "state": "claimed"},
                ],
                "reload_requests": [{"id": "r-1", "state": "dispatched"}],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(state_path)
    assert len(residual) == 3
    assert any("task-1" in item and "queued" in item for item in residual)
    assert any("task-2" in item and "claimed" in item for item in residual)
    assert any("r-1" in item and "dispatched" in item for item in residual)


def test_bridge_state_lease_preflight_accepts_terminal_states(tmp_path: Path) -> None:
    """C-118 Gap 4 positive: terminal task and reload states are not residual —
    a bridge with only succeeded/blocked/failed/cancelled tasks and applied/
    failed/expired reorders is isolated."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [
                    {"id": "t1", "state": "succeeded"},
                    {"id": "t2", "state": "blocked"},
                    {"id": "t3", "state": "failed"},
                    {"id": "t4", "state": "cancelled"},
                ],
                "reload_requests": [
                    {"id": "r1", "state": "applied"},
                    {"id": "r2", "state": "failed"},
                    {"id": "r3", "state": "expired"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert gate._bridge_state_lease_preflight(state_path) == []


def test_bridge_state_lease_preflight_fails_closed_on_missing_file(
    tmp_path: Path,
) -> None:
    """C-118 Gap 4 counter-example: a configured-but-missing bridge-state file
    cannot prove lease isolation and must fail closed."""
    missing = tmp_path / "does-not-exist.json"
    residual = gate._bridge_state_lease_preflight(missing)
    assert residual
    assert "missing" in residual[0]


def test_bridge_state_lease_preflight_fails_closed_on_bad_schema(
    tmp_path: Path,
) -> None:
    """C-122 Fix 2 counter-examples: an empty object, a wrong schema version,
    non-array tasks/reload_requests and an unknown task state each fail closed —
    none of them can prove bridge lease isolation and none is silently skipped."""
    # Empty object.
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    residual = gate._bridge_state_lease_preflight(empty)
    assert any("empty object" in item for item in residual)
    # Wrong schema version.
    wrong_schema = tmp_path / "wrong-schema.json"
    wrong_schema.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v1",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(wrong_schema)
    assert any("schema_version" in item for item in residual)
    # tasks is not an array.
    tasks_not_array = tmp_path / "tasks-not-array.json"
    tasks_not_array.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": {"id": "x", "state": "queued"},
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(tasks_not_array)
    assert any("tasks is not an array" in item for item in residual)
    # reload_requests is not an array.
    reloads_not_array = tmp_path / "reloads-not-array.json"
    reloads_not_array.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": {"id": "r", "state": "queued"},
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(reloads_not_array)
    assert any("reload_requests is not an array" in item for item in residual)
    # Unknown task state.
    unknown_state = tmp_path / "unknown-state.json"
    unknown_state.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [{"id": "t1", "state": "in_transit"}],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(unknown_state)
    assert any("unknown state 'in_transit'" in item for item in residual)
    # Unknown reload state.
    unknown_reload = tmp_path / "unknown-reload.json"
    unknown_reload.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [{"id": "r1", "state": "reloading"}],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(unknown_reload)
    assert any("unknown state 'reloading'" in item for item in residual)


def test_bridge_state_lease_preflight_naive_saved_at_fails_closed(
    tmp_path: Path,
) -> None:
    """C-122 round-18 gate-5 counter-example: a NAIVE ``saved_at`` (bare wall
    clock, no UTC offset) cannot prove the persisted bridge state is current and
    fails closed — the real v2 schema requires a timezone-aware timestamp
    (``browser_bridge._require_timezone``), so a naive one is residual, never
    silently accepted."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T12:00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(state_path)
    assert any(
        "saved_at" in item and "timezone-aware" in item for item in residual
    )


def test_bridge_state_lease_preflight_missing_saved_at_fails_closed(
    tmp_path: Path,
) -> None:
    """C-122 round-18 gate-5 counter-example: a bridge-state file with NO
    ``saved_at`` timestamp cannot prove it is current and fails closed."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(state_path)
    assert any(
        "saved_at" in item and "timezone-aware" in item for item in residual
    )


def test_bridge_state_lease_preflight_positive_offset_saved_at_accepted(
    tmp_path: Path,
) -> None:
    """C-122 round-18 gate-5: a ``saved_at`` carrying a POSITIVE UTC offset
    (``+08:00`` — the host live-state zone) is timezone-aware and passes the
    timezone gate; only the wall-clock-relabelling comparison would misjudge it.
    The presence of the offset is what certifies the timestamp is comparable."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T20:00:00+08:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_lease_preflight(state_path)
    assert not any("saved_at" in item for item in residual)


def test_bridge_state_lease_preflight_default_binds_live_runtime_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 Fix 2: with no env var set, the preflight resolves to the live
    API's own ``.runtime/browser-bridge-state.json`` — a missing default file
    fails closed (it cannot prove lease isolation), it never passes vacuously."""
    monkeypatch.delenv(gate._BRIDGE_STATE_ENV, raising=False)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    residual = gate._bridge_state_lease_preflight()
    assert residual
    assert "missing" in residual[0]
    assert str(tmp_path / ".runtime" / "browser-bridge-state.json") in residual[0]


def test_resolve_bridge_state_path_honors_explicit_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-118 Gap 4 wiring: an explicit override wins; otherwise the
    ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH`` env var decides; with neither set
    the preflight still binds to the live API's own
    ``.runtime/browser-bridge-state.json`` (C-122 Fix 2) — never a vacuous
    pass."""
    explicit = tmp_path / "explicit.json"
    assert gate._resolve_bridge_state_path(explicit) == explicit
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(tmp_path / "env.json"))
    assert gate._resolve_bridge_state_path() == tmp_path / "env.json"
    monkeypatch.delenv(gate._BRIDGE_STATE_ENV)
    assert gate._resolve_bridge_state_path() == gate.ROOT / ".runtime" / "browser-bridge-state.json"


def test_layer6_fails_when_bridge_state_holds_residual_work(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    tmp_path: Path,
    staging_dir: Path,
) -> None:
    """C-118 Gap 4 integration: layer 6 refuses a fresh E2E when the persisted
    bridge-state JSON holds queued/claimed work even with a clean planning DB."""
    staging_dir.mkdir()
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [])
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [{"id": "task-x", "state": "queued"}],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(state_path))
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")
    start = _expected_snapshot(clean_repo)
    result = gate.layer6_full_e2e(staging_dir, start, live_state_db=db_path)
    assert result.passed is False
    assert "bridge" in result.detail


# ---------------------------------------------------------------------------
# C-122 round-18 item 6: post-run bridge-state postcheck (second read-only
# snapshot; the run must leave no residual queued/claimed/reload behind)
# ---------------------------------------------------------------------------


def test_bridge_state_postcheck_accepts_terminal_states(tmp_path: Path) -> None:
    """C-122 round-18 item 6 positive: a clean post-run bridge state (only
    terminal task/reload states) is not residual — the run consumed its lease."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [{"id": "t1", "state": "succeeded"}],
                "reload_requests": [{"id": "r1", "state": "applied"}],
            }
        ),
        encoding="utf-8",
    )
    assert gate._bridge_state_postcheck(state_path) == []
    assert gate._BRIDGE_STATE_SNAPSHOT_AFTER is not None
    assert gate._BRIDGE_STATE_SNAPSHOT_AFTER["file"] == state_path.name
    assert gate._BRIDGE_STATE_SNAPSHOT_AFTER["residual"] == []


def test_bridge_state_postcheck_fails_closed_on_missing_file(
    tmp_path: Path,
) -> None:
    """C-122 round-18 item 6 counter-example: a missing post-run bridge-state
    file cannot prove the run left no residual and fails closed."""
    missing = tmp_path / "does-not-exist.json"
    residual = gate._bridge_state_postcheck(missing)
    assert residual
    assert "missing" in residual[0]


def test_bridge_state_postcheck_detects_queued_work(tmp_path: Path) -> None:
    """C-122 round-18 item 6 counter-example: queued/claimed tasks or pending
    reorder left after the run are residual — the run did not consume its lease."""
    state_path = tmp_path / "bridge-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [{"id": "task-x", "state": "queued"}],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    residual = gate._bridge_state_postcheck(state_path)
    assert any("queued" in item for item in residual)
    assert gate._BRIDGE_STATE_SNAPSHOT_AFTER is not None
    assert gate._BRIDGE_STATE_SNAPSHOT_AFTER["residual"] == residual


def test_bridge_state_postcheck_keeps_separate_snapshot(tmp_path: Path) -> None:
    """C-122 round-18 item 6: the post-run snapshot is stored separately from the
    pre-run snapshot — one never overwrites the other, so the compact can certify
    the isolation proof from BOTH sides of the run."""
    clean = tmp_path / "before.json"
    clean.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    dirty = tmp_path / "after.json"
    dirty.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [{"id": "task-x", "state": "queued"}],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    gate._BRIDGE_STATE_SNAPSHOT = None
    gate._BRIDGE_STATE_SNAPSHOT_AFTER = None
    try:
        gate._bridge_state_lease_preflight(clean)
        gate._bridge_state_postcheck(dirty)
        assert gate._BRIDGE_STATE_SNAPSHOT is not None
        assert gate._BRIDGE_STATE_SNAPSHOT["residual"] == []
        assert gate._BRIDGE_STATE_SNAPSHOT_AFTER is not None
        assert any("queued" in item for item in gate._BRIDGE_STATE_SNAPSHOT_AFTER["residual"])
    finally:
        gate._BRIDGE_STATE_SNAPSHOT = None
        gate._BRIDGE_STATE_SNAPSHOT_AFTER = None


def test_layer6_fails_when_postcheck_finds_residual_work(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    tmp_path: Path,
    staging_dir: Path,
) -> None:
    """C-122 round-18 item 6 integration: the pre-run bridge state is clean (the
    preflight passes and the E2E runs), but the run leaves queued work behind —
    the post-run postcheck then fails the layer closed."""
    staging_dir.mkdir()
    db_path = tmp_path / "live.db"
    _make_jobs_db(db_path, [])
    bridge_state_path = tmp_path / "bridge-state.json"

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        # Model a run that leaves a queued bridge task behind (orphaned work).
        bridge_state_path.write_text(
            json.dumps(
                {
                    "schema_version": "tripchord-browser-bridge-state-v2",
                    "saved_at": "2026-08-10T00:00:00+00:00",
                    "tasks": [{"id": "orphan-1", "state": "queued"}],
                    "reload_requests": [],
                }
            ),
            encoding="utf-8",
        )
        return 0, ""

    # A clean pre-run state: the preflight must pass so the layer reaches the
    # runner, then the postcheck (after fake_run) reads the mutated file.
    bridge_state_path.write_text(
        json.dumps(
            {
                "schema_version": "tripchord-browser-bridge-state-v2",
                "saved_at": "2026-08-10T00:00:00+00:00",
                "tasks": [],
                "reload_requests": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(gate._BRIDGE_STATE_ENV, str(bridge_state_path))
    monkeypatch.setattr(gate, "_bridge_token", lambda: "B" * 64)
    monkeypatch.setenv("TRIPCHORD_ACK_MODEL_COST", "1")
    monkeypatch.setattr(gate, "_run", fake_run)
    monkeypatch.setattr(gate, "_runner_revision_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_runtime_provenance_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(gate, "_extract_build_fingerprint", lambda *a, **k: None)
    start = _expected_snapshot(clean_repo)
    (staging_dir / "live-done-gate-v4.json").write_text(
        json.dumps({"run_status": "completed", "done_gate": _matching_done_gate()}),
        encoding="utf-8",
    )
    result = gate.layer6_full_e2e(staging_dir, start, live_state_db=db_path)
    assert result.passed is False
    assert "postcheck" in result.detail


def test_main_rejects_staging_symlink(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, tmp_path: Path
) -> None:
    """C-118 Gap 6 counter-example: a ``--staging-dir`` that is a symlink is
    rejected with exit 2 before any evidence is created — the lstat-based
    conflict check never follows a planted symlink to attacker-chosen bytes."""
    _patch_root(monkeypatch, clean_repo)
    target = tmp_path / "real-staging"
    target.mkdir()
    symlink = tmp_path / "staging-link"
    symlink.symlink_to(target, target_is_directory=True)
    before = _porcelain(clean_repo)
    rc = gate.main(["--staging-dir", str(symlink), "--quiet"])
    assert rc == 2
    assert _porcelain(clean_repo) == before
    assert not (symlink / "product-v1-done-gate.json").exists()


def _layer5_compact_fixture() -> dict[str, object]:
    """A layer-5 compact that satisfies the strong C-118 blob contract: the
    registry-derived certified canary scopes (five browser Companion OTA + iCom
    public-API, 6 total), exact coverage thresholds, all
    passed/fresh/authorized/read-only, and the connected Companion authorizing
    exactly the five certified browser scopes (C-122 round-19)."""
    canary = _matching_canary()
    cs = canary["companion_status"]
    companions = [
        {
            "companion_id": comp["companion_id"],
            "providers": comp["providers"],
            "authorized_scope_keys": comp["authorized_scope_keys"],
            "is_fresh": comp["is_fresh"],
            "age_seconds": comp["age_seconds"],
            "build_sha256": (comp.get("build_identity") or {}).get("build_sha256"),
        }
        for comp in cs["companions"]
    ]
    expected = sorted(gate._ALL_CERTIFIED_CANARY_SCOPES)
    return {
        "schema_version": "tripchord-done-gate-layer5-compact-v2",
        "generated_at": "2026-08-10T00:00:00+00:00",
        "passed": canary["passed"],
        "bridge_token_present": canary["bridge_token_present"],
        "coverage": {
            "expected_scope_count": len(expected),
            "expected_scopes": expected,
            "observed_scope_count": len(expected),
            "passed_scope_count": len(expected),
            "missing": [],
        },
        "scopes": canary["scopes"],
        "companion_status": {
            "status": cs["status"],
            "stale_after_seconds": cs["stale_after_seconds"],
            "companions": companions,
        },
        "raw_evidence": {
            "file": "live-canary-certified.json",
            "committed": False,
            "sha256": "a" * 64,
        },
    }


def test_verify_layer5_compact_contract_accepts_full_set() -> None:
    """C-118 Gap 7 positive: the complete registry-derived six-scope layer-5
    compact passes the blob read-back contract."""
    gate._verify_layer5_compact_contract(
        "done-gate-layer5-compact.json", _layer5_compact_fixture()
    )


def test_verify_layer5_compact_contract_accepts_connected_browser_companion() -> None:
    """C-122 round-19 counter-example: a connected ctrip/qunar/tongcheng Companion
    authorizing EXACTLY the certified browser OTA scopes (five scopes, excluding
    ``icom:transfer`` and excluding the DISABLED ``tongcheng:lodging``) genuinely
    passes — the layer-5 compact contract accepts the registry-derived certified
    canary scope set and the five-scope Companion authorization together."""
    compact = _layer5_compact_fixture()
    companions = compact["companion_status"]["companions"]  # type: ignore[assignment]
    assert len(companions) == 1
    assert set(companions[0]["authorized_scope_keys"]) == set(
        gate._CERTIFIED_OTA_SCOPES
    )
    assert "icom:transfer" not in companions[0]["authorized_scope_keys"]
    assert "tongcheng:lodging" not in companions[0]["authorized_scope_keys"]
    # The full registry-derived canary + Companion contract passes.
    gate._verify_layer5_compact_contract(
        "done-gate-layer5-compact.json", compact
    )


def test_verify_layer5_compact_contract_rejects_companion_with_icom_scope() -> None:
    """C-122 HG-A counter-example: a Companion whose ``authorized_scope_keys``
    wrongly includes the iCom public-API scope must fail the compact contract —
    ``icom:transfer`` is never a Companion authorization."""
    compact = _layer5_compact_fixture()
    companions = compact["companion_status"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) | {"icom:transfer"}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_companion_missing_certified_scope() -> None:
    """C-122 round-19 counter-example: a Companion that does not authorize a
    CERTIFIED browser scope must fail the compact contract — the registry-derived
    browser scope set is mandatory and cannot silently shrink."""
    compact = _layer5_compact_fixture()
    removed = sorted(gate._CERTIFIED_OTA_SCOPES)[0]
    companions = compact["companion_status"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) - {removed}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_disabled_scope() -> None:
    """C-122 round-19 (2026-08-11 17:03 veto) counter-example: the DISABLED scope
    ``tongcheng:lodging`` (host_permissions/allowed_actions empty, concurrency=0)
    must NEVER enter the canary compact — a compact that swaps a certified scope
    for the disabled scope, or a Companion that authorizes it, fails closed."""
    # (a) the compact scope list may not include the disabled scope
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "tongcheng:lodging",
        "kind": "companion_heartbeat",
        "provider": "tongcheng",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {"companion_id": "comp-1"},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="not one of the certified canary scopes"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )
    # (b) a Companion authorizing the disabled scope fails the exact browser set
    compact = _layer5_compact_fixture()
    companions = compact["companion_status"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) | {"tongcheng:lodging"}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_incomplete_scope_set() -> None:
    """C-118 Gap 7 counter-example: a compact missing a certified scope (coverage
    reports it missing, the scope list is short) must fail the phase closed."""
    compact = _layer5_compact_fixture()
    expected = sorted(gate._ALL_CERTIFIED_CANARY_SCOPES)
    compact["coverage"] = {  # type: ignore[assignment]
        "expected_scope_count": len(expected),
        "expected_scopes": expected,
        "observed_scope_count": len(expected) - 1,
        "passed_scope_count": len(expected) - 1,
        "missing": ["ctrip:lodging"],
    }
    compact["scopes"] = compact["scopes"][:-1]  # type: ignore[assignment]
    with pytest.raises(
        gate.GateStateChangedError,
        match=f"observed_scope_count != {len(expected)}",
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_non_certified_scope() -> None:
    """C-118 Gap 7 counter-example: a compact whose scope set is not exactly the
    registry-derived certified canary scopes (an ad-hoc scope swaps in for a
    certified one) fails."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:car",
        "kind": "companion_heartbeat",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {"companion_id": "comp-1"},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="not one of the certified canary scopes"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def _layer6_compact_fixture() -> dict[str, object]:
    """A layer-6 compact that satisfies the strong C-118/C-122 blob contract:
    the fifteen done-gate checks all passed, each carrying its structured
    per-item evidence, plus the repo / runtime / Companion identity, the
    candidate-set / scenario SHA bindings and the event-injection / timeout /
    runner contracts."""
    done_gate = _matching_done_gate()
    done_gate["check_count"] = 15  # type: ignore[index]
    done_gate["passed_check_count"] = 15  # type: ignore[index]
    return {
        "schema_version": "tripchord-done-gate-layer6-compact-v2",
        "run_status": "completed",
        "done_gate": done_gate,
        "repo_revision": {
            "branch": "main",
            "commit_sha": "a" * 40,
            "worktree_dirty": False,
        },
        "runtime_before_run": {
            "model_provider": "test-provider",
            "primary_model": "test-model",
            "runtime_provenance": {
                "commit_sha": "a" * 40,
            },
        },
        "companion_preflight": {
            "status": "connected",
            "stale_after_seconds": 45,
            "companions": [
                {
                    "companion_id": "comp-1",
                    # C-122 round-19: exactly the CERTIFIED BROWSER Companion OTA
                    # scopes (five scopes, registry-derived); ``icom:transfer`` is
                    # a public-API scope and never appears in a Companion's
                    # authorization set, and the DISABLED ``tongcheng:lodging``
                    # never enters the compact.
                    "authorized_scope_keys": sorted(gate._CERTIFIED_OTA_SCOPES),
                }
            ],
        },
        "api_payload_candidate_set_sha256": "a" * 64,
        # C-122 round-19 (gap 4): the raw request payload SHA the checkpoint
        # binding's request identity must bind to (matches the fixture's
        # checkpoint request_sha256).
        "api_payload_sha256": _FIXTURE_REQUEST_SHA256,
        "scenario_sha256": "a" * 64,
        "event_injection_contract": {
            "mode": "synthetic_sold_out_fault_injection",
            "source": "tripchord-done-gate-synthetic-fault",
        },
        "timeout_contract": {"server_execution_timeout_seconds": 3600},
        "runner_contract": {"require_model_enhancement": True},
        "bridge_state_lease_preflight": {
            # A repo-relative identifier whose file does not exist in the real
            # repo tree, so the contract's live-recompute check is skipped for
            # the fixture (dedicated tests exercise recompute with their own
            # file under a monkeypatched ROOT).
            "file": ".runtime/done-gate-test-fixture-bridge-state.json",
            "sha256": "a" * 64,
            "residual": [],
        },
        # C-122 round-18 item 6: the POST-run lease binding is contract-required
        # too — the run must leave no residual queued/claimed/reload behind.
        "bridge_state_lease_postcheck": {
            "file": ".runtime/done-gate-test-fixture-bridge-state.json",
            "sha256": "a" * 64,
            "residual": [],
        },
    }


def test_verify_layer6_compact_contract_accepts_full_passing_set() -> None:
    """C-118 Gap 7 positive: the full fifteen-check layer-6 compact passes the
    blob read-back contract."""
    gate._verify_layer6_compact_contract(
        "done-gate-layer6-compact.json",
        _layer6_compact_fixture(),
        tested_commit_sha="a" * 40,
    )


def test_verify_layer6_compact_contract_rejects_non_passing_check() -> None:
    """C-118 Gap 7 counter-example: a compact with one non-passed done-gate
    check must fail the phase closed."""
    compact = _layer6_compact_fixture()
    compact["done_gate"]["checks"][0]["passed"] = False  # type: ignore[index]
    with pytest.raises(gate.GateStateChangedError, match="not passed"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_identity() -> None:
    """C-118 Gap 7 counter-example: a compact that drops the repo/runtime/
    Companion identity or a binding contract fails closed even when the checks
    all pass."""
    compact = _layer6_compact_fixture()
    del compact["runner_contract"]
    with pytest.raises(gate.GateStateChangedError, match="runner_contract"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_bridge_binding() -> None:
    """C-122 Fix 2 counter-example: a compact that drops the bridge-state
    lease-preflight binding (the checked file path + SHA256) fails closed even
    when the fifteen checks all pass — the isolation proof must be in the trail."""
    compact = _layer6_compact_fixture()
    del compact["bridge_state_lease_preflight"]
    with pytest.raises(
        gate.GateStateChangedError, match="bridge_state_lease_preflight"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_postcheck_binding() -> None:
    """C-122 round-18 item 6 counter-example: a compact that drops the POST-run
    bridge-state lease binding fails closed even when the pre-run binding and
    all fifteen checks pass — the run must prove it consumed its lease."""
    compact = _layer6_compact_fixture()
    del compact["bridge_state_lease_postcheck"]
    with pytest.raises(
        gate.GateStateChangedError, match="bridge_state_lease_postcheck"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_postcheck_residual() -> None:
    """C-122 round-18 item 6 counter-example: a post-run bridge-state binding
    that records residual queued/claimed work fails closed — the E2E must not
    leave in-flight bridge work behind."""
    compact = _layer6_compact_fixture()
    compact["bridge_state_lease_postcheck"] = {
        "file": ".runtime/done-gate-test-fixture-bridge-state.json",
        "sha256": "a" * 64,
        "residual": ["bridge task task-x state=queued is queued/claimed"],
    }
    with pytest.raises(
        gate.GateStateChangedError, match="residual is not an empty list"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_bridge_state_binding_preflight_is_capture_time_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 HG-B counter-example: a PRE-flight binding whose recorded SHA does
    not match the CURRENT live bridge-state file is still valid — the preflight
    sealed the capture-time bytes and the E2E run legitimately advanced the file
    while holding its lease.  ``compare_current=False`` must accept it, and
    ``compare_current=True`` must reject it (proving the live-file comparison is
    gated to the post-check only)."""
    _patch_root(monkeypatch, tmp_path)
    compact = _layer6_compact_fixture()
    rel = ".runtime/done-gate-test-fixture-bridge-state.json"
    live = tmp_path / rel
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b'{"schema_version": "tripchord-browser-bridge-state-v2"}')
    # Preflight is a capture-time snapshot: NOT recomputed against the live file.
    gate._verify_bridge_state_binding(
        compact,
        "done-gate-layer6-compact.json",
        "bridge_state_lease_preflight",
        compare_current=False,
    )
    # If it were compared, the recorded "a"*64 sha would fail closed.
    with pytest.raises(
        gate.GateStateChangedError, match="does not match the current"
    ):
        gate._verify_bridge_state_binding(
            compact,
            "done-gate-layer6-compact.json",
            "bridge_state_lease_preflight",
            compare_current=True,
        )


def test_verify_bridge_state_binding_postcheck_must_match_current_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 HG-B counter-example: a POST-check binding whose recorded SHA does
    not match the CURRENT live bridge-state file fails closed — the postcheck IS
    the post-run state and must equal the current file bytes."""
    _patch_root(monkeypatch, tmp_path)
    compact = _layer6_compact_fixture()
    rel = ".runtime/done-gate-test-fixture-bridge-state.json"
    live = tmp_path / rel
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b'{"schema_version": "tripchord-browser-bridge-state-v2"}')
    with pytest.raises(
        gate.GateStateChangedError, match="does not match the current"
    ):
        gate._verify_bridge_state_binding(
            compact,
            "done-gate-layer6-compact.json",
            "bridge_state_lease_postcheck",
            compare_current=True,
        )


def test_verify_layer6_compact_contract_accepts_preflight_advanced_by_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-122 HG-B E2E counter-example: a compact whose PRE-flight SHA differs
    from the current live file but whose POST-check SHA MATCHES the current file
    passes the full layer-6 contract — the run advanced the bridge state while
    holding its lease, then left it in exactly the post-run state it recorded."""
    _patch_root(monkeypatch, tmp_path)
    compact = _layer6_compact_fixture()
    rel = ".runtime/done-gate-test-fixture-bridge-state.json"
    live = tmp_path / rel
    live.parent.mkdir(parents=True, exist_ok=True)
    live_bytes = b'{"schema_version": "tripchord-browser-bridge-state-v2"}'
    live.write_bytes(live_bytes)
    actual_sha = gate._sha256_file(live)
    # The postcheck records the ACTUAL current bytes; the preflight keeps its
    # distinct (capture-time) hash the run has since advanced past.
    compact["bridge_state_lease_postcheck"]["sha256"] = actual_sha  # type: ignore[index]
    gate._verify_layer6_compact_contract(
        "done-gate-layer6-compact.json",
        compact,
        tested_commit_sha="a" * 40,
    )


def test_verify_layer6_compact_contract_rejects_empty_per_check_evidence() -> None:
    """C-122 Fix 3 counter-example: a compact that reduces a done-gate check to a
    bare verdict (empty per-item evidence) must fail the phase closed."""
    compact = _layer6_compact_fixture()
    compact["done_gate"]["checks"][1]["evidence"] = {}  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="no per-item structured evidence"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_binding_field() -> None:
    """C-122 Fix 3 counter-example: a check whose structured evidence drops a
    required recomputable binding field fails closed."""
    compact = _layer6_compact_fixture()
    del compact["done_gate"]["checks"][0]["evidence"]["candidate_set_sha256"]  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="missing required binding field"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_wrong_candidate_sha() -> None:
    """C-122 Fix 3 counter-example: a malformed (non-64-hex) candidate-set SHA in
    the compact fails closed — the binding must be well-formed to be recomputable."""
    compact = _layer6_compact_fixture()
    compact["api_payload_candidate_set_sha256"] = "not-a-sha"  # type: ignore[assignment]
    with pytest.raises(
        gate.GateStateChangedError, match="not a valid sha256"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_mismatched_candidate_sha() -> None:
    """C-122 Fix 3 counter-example: the prefrozen candidate-set binding in the
    compact disagreeing with the top-level api_payload_candidate_set_sha256 fails
    closed — a wrong SHA can never certify the frozen candidate set."""
    compact = _layer6_compact_fixture()
    compact["done_gate"]["checks"][0]["evidence"]["candidate_set_sha256"] = "b" * 64  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="does not match api_payload"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_dropped_source_graph_bindings() -> None:
    """C-122 round-18 HG-E counter-example: a compact whose v4_source_graph
    evidence drops any of the four query/plan bindings (expected_query_shapes,
    expected_icom_task_ids, pair_ids, total_planned_task_count) fails closed —
    those recomputable bindings are required, not optional.  C-122 supervision
    01:10 adds ``checkpoint_bound_pair_ids`` to the required set: the compact
    must carry the run's independent checkpoint-bound sealed pair ids."""
    for dropped in (
        "expected_query_shapes",
        "expected_icom_task_ids",
        "pair_ids",
        "total_planned_task_count",
        "checkpoint_bound_pair_ids",
    ):
        compact = _layer6_compact_fixture()
        del compact["done_gate"]["checks"][1]["evidence"][dropped]  # type: ignore[index]
        with pytest.raises(
            gate.GateStateChangedError, match="missing required binding field"
        ):
            gate._verify_layer6_compact_contract(
                "done-gate-layer6-compact.json",
                compact,
                tested_commit_sha="a" * 40,
            )


def test_verify_layer6_compact_contract_rejects_dropped_coverage_bindings() -> None:
    """C-122 round-18 HG-E counter-example: a compact whose
    strict_selected_plan_platform_coverage evidence drops coverage_mode or
    all_platforms_complete fails closed — a bare provider list cannot prove the
    strict full-completion receipt."""
    for dropped in ("coverage_mode", "all_platforms_complete"):
        compact = _layer6_compact_fixture()
        del compact["done_gate"]["checks"][11]["evidence"][dropped]  # type: ignore[index]
        with pytest.raises(
            gate.GateStateChangedError, match="missing required binding field"
        ):
            gate._verify_layer6_compact_contract(
                "done-gate-layer6-compact.json",
                compact,
                tested_commit_sha="a" * 40,
            )


def test_verify_layer6_compact_contract_rejects_all_platforms_not_complete() -> None:
    """C-122 round-18 HG-E counter-example: a passing strict-coverage receipt
    whose all_platforms_complete is not true contradicts the verdict and fails
    closed."""
    compact = _layer6_compact_fixture()
    compact["done_gate"]["checks"][11]["evidence"]["all_platforms_complete"] = False  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="all_platforms_complete is not true"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_empty_coverage_mode() -> None:
    """C-122 round-18 HG-E counter-example: an empty coverage_mode cannot back a
    passing strict-coverage receipt."""
    compact = _layer6_compact_fixture()
    compact["done_gate"]["checks"][11]["evidence"]["coverage_mode"] = ""  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError,
        match=r"coverage_mode.*is empty|coverage_mode is missing or empty",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_accepts_hg_e_bindings() -> None:
    """C-122 round-18 HG-E positive: the full fixture carries all six HG-E
    bindings and still passes the strong compact contract."""
    compact = _layer6_compact_fixture()
    v4 = compact["done_gate"]["checks"][1]["evidence"]  # type: ignore[index]
    assert v4["expected_query_shapes"]
    assert v4["expected_icom_task_ids"]
    assert v4["pair_ids"]
    assert v4["checkpoint_bound_pair_ids"]
    assert isinstance(v4["total_planned_task_count"], int) and v4["total_planned_task_count"] > 0
    strict = compact["done_gate"]["checks"][11]["evidence"]  # type: ignore[index]
    assert strict["coverage_mode"]
    assert strict["all_platforms_complete"] is True
    gate._verify_layer6_compact_contract(
        "done-gate-layer6-compact.json",
        compact,
        tested_commit_sha="a" * 40,
    )


def test_verify_layer6_compact_contract_rejects_bad_commit_sha() -> None:
    """C-122 Fix 3 counter-example: a compact whose repo/runtime git SHA is not a
    valid 40-hex commit fails closed."""
    compact = _layer6_compact_fixture()
    compact["repo_revision"]["commit_sha"] = "deadbeef"  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="40-hex git SHA"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_not_completed() -> None:
    """C-122 Fix 3 counter-example: a compact that does not claim run_status
    completed cannot certify a passing gate."""
    compact = _layer6_compact_fixture()
    compact["run_status"] = "done_gate_failed"  # type: ignore[assignment]
    with pytest.raises(
        gate.GateStateChangedError, match="run_status"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_bad_companion_preflight() -> None:
    """C-122 Fix 3 counter-example: a compact whose Companion preflight lacks the
    freshness window or any companion identity fails closed."""
    compact = _layer6_compact_fixture()
    compact["companion_preflight"] = {"status": "ok", "companions": []}  # type: ignore[assignment]
    with pytest.raises(
        gate.GateStateChangedError, match="stale_after_seconds"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_companion_with_icom_scope() -> None:
    """C-122 HG-A counter-example: the layer-6 Companion preflight whose
    ``authorized_scope_keys`` wrongly includes the iCom public-API scope fails
    closed — a Companion authorizes exactly the certified browser OTA scopes."""
    compact = _layer6_compact_fixture()
    companions = compact["companion_preflight"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) | {"icom:transfer"}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_companion_missing_certified_scope() -> None:
    """C-122 round-19 counter-example: the layer-6 Companion preflight that does
    not authorize a CERTIFIED browser scope fails closed — the registry-derived
    browser scope set is mandatory and cannot silently shrink."""
    compact = _layer6_compact_fixture()
    removed = sorted(gate._CERTIFIED_OTA_SCOPES)[0]
    companions = compact["companion_preflight"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) - {removed}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_disabled_scope() -> None:
    """C-122 round-19 (2026-08-11 17:03 veto) counter-example: the DISABLED scope
    ``tongcheng:lodging`` must never enter the layer-6 companion preflight — a
    Companion that authorizes it fails closed."""
    compact = _layer6_compact_fixture()
    companions = compact["companion_preflight"]["companions"]  # type: ignore[assignment]
    companions[0]["authorized_scope_keys"] = sorted(
        set(gate._CERTIFIED_OTA_SCOPES) | {"tongcheng:lodging"}
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match="authorized_scope_keys != the certified browser Companion OTA scopes",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_bad_event_contract() -> None:
    """C-122 Fix 3 counter-example: a compact whose event-injection contract is
    reduced to an empty object fails closed."""
    compact = _layer6_compact_fixture()
    compact["event_injection_contract"] = {}  # type: ignore[assignment]
    with pytest.raises(
        gate.GateStateChangedError, match="event_injection_contract lacks a mode"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_schema_version_mismatch() -> None:
    """C-122 acceptance counter-example: a compact claiming an older or unknown
    schema version must fail closed — the producer and validator must share the
    same schema version, or the committed evidence cannot be trusted."""
    compact = _layer6_compact_fixture()
    compact["schema_version"] = "tripchord-done-gate-layer6-compact-v1"  # type: ignore[assignment]
    with pytest.raises(gate.GateStateChangedError, match="schema_version"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_repo_runtime_sha_mismatch() -> None:
    """C-122 acceptance counter-example: repo_revision.commit_sha disagreeing
    with runtime_provenance.commit_sha fails closed — the runtime identity must
    name the same revision the compact claims to have tested."""
    compact = _layer6_compact_fixture()
    compact["runtime_before_run"]["runtime_provenance"]["commit_sha"] = "b" * 40  # type: ignore[index]
    with pytest.raises(gate.GateStateChangedError, match="repo_revision"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_repo_tested_sha_mismatch() -> None:
    """C-122 acceptance counter-example: repo_revision.commit_sha disagreeing
    with the run's tested_commit_sha (S) fails closed — the compact must bind the
    exact code that was exercised."""
    compact = _layer6_compact_fixture()
    compact["repo_revision"]["commit_sha"] = "b" * 40  # type: ignore[index]
    compact["runtime_before_run"]["runtime_provenance"]["commit_sha"] = "b" * 40  # type: ignore[index]
    with pytest.raises(gate.GateStateChangedError, match="!= tested_commit_sha"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_candidate_sha() -> None:
    """C-122 acceptance counter-example: a compact that drops the candidate-set
    SHA binding fails closed — a missing candidate SHA voids the frozen-plan
    binding."""
    compact = _layer6_compact_fixture()
    del compact["api_payload_candidate_set_sha256"]  # type: ignore[misc]
    with pytest.raises(
        gate.GateStateChangedError, match="missing or not a valid sha256"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_scenario_sha() -> None:
    """C-122 acceptance counter-example: a compact that drops the scenario SHA
    binding fails closed."""
    compact = _layer6_compact_fixture()
    del compact["scenario_sha256"]  # type: ignore[misc]
    with pytest.raises(gate.GateStateChangedError, match="scenario_sha256"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


# --- semantic counter-examples (field existence is not enough) ----------------


def _layer6_compact_with_evidence_mutated(mutate: Any) -> dict[str, object]:
    """A passing layer-6 compact whose per-check evidence is mutated by
    ``mutate`` — every semantic counter-example keeps repo==runtime==S and all
    other checks intact, so the ONLY failure is the mutated semantic field."""
    compact = _layer6_compact_fixture()
    mutate(compact["done_gate"]["checks"])  # type: ignore[index]
    return compact


def test_verify_layer6_compact_contract_rejects_graph_chain_not_ok() -> None:
    """C-122 acceptance counter-example: a passing planner-verifier-repair check
    whose evidence records graph_chain_ok=false must fail closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "planner_verifier_repair_orchestrator":
                check["evidence"]["graph_chain_ok"] = False  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="graph_chain_ok"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_budget_mismatch() -> None:
    """C-122 acceptance counter-example: computed budget differing from the
    declared budget must fail closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "exact_budget_and_selected_evidence":
                check["evidence"]["computed_total_cents"] = 2000  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="computed_total_cents"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_dynamic_replan_not_passed() -> None:
    """C-122 acceptance counter-example: a dynamic replan sub-item recorded as
    not-passed inside a passing master check must fail closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "event_injection_repair_reverify_master":
                check["evidence"]["dynamic_replan"]["passed"] = False  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="dynamic_replan"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_exact_provider_below_threshold() -> None:
    """C-122 red-line counter-example: exact_provider_count below the dual-platform
    exact-quote threshold must fail closed — the evidence can never certify a
    passing gate."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "flight_search_outcome_contract":
                check["evidence"]["exact_provider_count"] = 0  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="exact_provider_count"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_pending_or_evil_provider() -> None:
    """C-122 acceptance counter-example: any provider outcome state that is not a
    terminal flight-search state (pending, evil, terminal, …) must fail closed."""
    for bad_state in ("pending", "evil", "terminal"):

        def mutate(checks: Any, state: str = bad_state) -> None:
            for check in checks:
                if check["name"] == "flight_search_outcome_contract":
                    check["evidence"]["provider_outcome_states"]["ctrip"] = state  # type: ignore[index]

        compact = _layer6_compact_with_evidence_mutated(mutate)
        with pytest.raises(gate.GateStateChangedError, match="outcome state"):
            gate._verify_layer6_compact_contract(
                "done-gate-layer6-compact.json",
                compact,
                tested_commit_sha="a" * 40,
            )


def test_verify_layer6_compact_contract_rejects_zero_browser_tasks() -> None:
    """C-122 round-18 gate-2 counter-example: a v4 source graph with a non-positive
    browser-task-per-pair count cannot prove the fixed per-pair query plan and
    fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_browser_tasks_per_pair"] = 0  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError, match="not a positive integer"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_duplicate_browser_source_ids() -> None:
    """C-122 round-18 gate-2 counter-example: duplicated expected browser Source
    ids are a forged plan and fail closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_browser_source_ids"] = [  # type: ignore[index]
                    "source-ctrip-flight",
                    "source-ctrip-flight",
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError, match="not unique"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_1pair_1task_graph() -> None:
    """C-122 HG-G counter-example (supervision real-run): a v4 source graph
    collapsed to 1 pair / 1 browser task / total=1 must REJECT — the compact
    must bind the frozen scenario's exact 3-pair set, per-pair counts and
    total=per-pair sum, never just non-empty / unique / positive values."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["pair_ids"] = ["pair-1"]  # type: ignore[index]
                check["evidence"]["expected_browser_tasks_per_pair"] = 1  # type: ignore[index]
                check["evidence"]["expected_browser_source_ids"] = ["source-ctrip-flight"]  # type: ignore[index]
                check["evidence"]["expected_query_shapes"] = ["ctrip:flight"]  # type: ignore[index]
                check["evidence"]["expected_icom_task_ids"] = ["public-transfer-icom-ctrip-1"]  # type: ignore[index]
                check["evidence"]["total_planned_task_count"] = 1  # type: ignore[index]
                check["evidence"]["per_pair"] = [  # type: ignore[index]
                    {
                        "pair_id": "pair-1",
                        "browser_source_task_count": 1,
                        "query_task_count": 1,
                        "icom_source_task_count": 1,
                    }
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="frozen scenario's exact",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_3pair_1task_graph() -> None:
    """C-122 round-18 supervision 16:03 counter-example A: a v4 source graph that
    keeps the frozen 3-pair set but shrinks EVERY pair to 1 browser/query/iCom
    task (3 pair x 1 task, total=3) must REJECT — the per-pair count must equal
    the frozen scenario's exact browser task count, never just be positive."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_browser_tasks_per_pair"] = 1  # type: ignore[index]
                check["evidence"]["expected_browser_source_ids"] = ["source-ctrip-flight"]  # type: ignore[index]
                check["evidence"]["expected_query_shapes"] = ["ctrip:flight"]  # type: ignore[index]
                check["evidence"]["expected_icom_task_ids"] = ["public-transfer-icom-ctrip-1"]  # type: ignore[index]
                check["evidence"]["total_planned_task_count"] = 3  # type: ignore[index]
                check["evidence"]["per_pair"] = [  # type: ignore[index]
                    {
                        "pair_id": "pair-1",
                        "browser_source_task_count": 1,
                        "query_task_count": 1,
                        "icom_source_task_count": 1,
                    },
                    {
                        "pair_id": "pair-2",
                        "browser_source_task_count": 1,
                        "query_task_count": 1,
                        "icom_source_task_count": 1,
                    },
                    {
                        "pair_id": "pair-3",
                        "browser_source_task_count": 1,
                        "query_task_count": 1,
                        "icom_source_task_count": 1,
                    },
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="frozen scenario's exact per-pair browser task count",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_foreign_pair_ids() -> None:
    """C-122 supervision 01:10 counter-example (foreign pair): a v4 source graph
    whose pair_ids are three arbitrary unique strings (``pair-1`` etc.) must
    REJECT even when the count (3), uniqueness, total and member sets all line
    up — every pair id must be a CANONICAL frozen-scenario ``date-pair:`` id
    (format + digest recomputed from the frozen constants).  This is the exact
    foreign-pair case the round-19 compact accepted (any 3 unique ids passed)."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["pair_ids"] = ["pair-1", "pair-2", "pair-3"]  # type: ignore[index]
                check["evidence"]["checkpoint_bound_pair_ids"] = [  # type: ignore[index]
                    "pair-1",
                    "pair-2",
                    "pair-3",
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="is not a canonical frozen-scenario date-pair id",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_2030_departure() -> None:
    """C-122 supervision 18:13 counter-example (2030): a v4 source graph whose
    pair ids are well-formed ``date-pair:`` ids with recomputing digests but a
    departure OUTSIDE the frozen August-2026 window must REJECT — the canonical
    TIME CONTRACT (departure within [2026-08-01, 2026-08-31]) is part of pair-id
    validity, enforced at acceptance exactly like the digest."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                future = _frozen_v4_fixture_pair_id("2030-01-01", "2030-01-06")
                check["evidence"]["pair_ids"] = [  # type: ignore[index]
                    future,
                    _FIXTURE_PAIR_IDS[1],
                    _FIXTURE_PAIR_IDS[2],
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="is not a canonical frozen-scenario date-pair id",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_reversed_dates() -> None:
    """C-122 supervision 18:13 counter-example (reversed dates): a well-formed
    pair id whose return date is NOT after its departure must REJECT — the
    canonical time contract requires ``return > departure``."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                reversed_pair = _frozen_v4_fixture_pair_id(
                    "2026-08-20", "2026-08-15"
                )
                check["evidence"]["pair_ids"] = [  # type: ignore[index]
                    reversed_pair,
                    _FIXTURE_PAIR_IDS[1],
                    _FIXTURE_PAIR_IDS[2],
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="is not a canonical frozen-scenario date-pair id",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


@pytest.mark.parametrize(
    ("departure", "return_date", "nights"),
    [
        ("2026-08-11", "2026-08-12", 1),
        ("2026-08-11", "2026-08-20", 9),
        ("2026-08-11", "2026-08-21", 10),
    ],
)
def test_verify_layer6_compact_contract_rejects_out_of_contract_nights(
    departure: str, return_date: str, nights: int
) -> None:
    """C-122 supervision 18:13 counter-example (1/9/10 nights): a well-formed
    pair id whose stay length is outside the frozen 5-8 night contract must
    REJECT — only five-to-eight-night pairs are canonical frozen-scenario
    pairs."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                bad_nights = _frozen_v4_fixture_pair_id(departure, return_date)
                check["evidence"]["pair_ids"] = [  # type: ignore[index]
                    bad_nights,
                    _FIXTURE_PAIR_IDS[1],
                    _FIXTURE_PAIR_IDS[2],
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="is not a canonical frozen-scenario date-pair id",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_reordered_checkpoint_chain() -> None:
    """C-122 supervision 18:13 counter-example (reordered chain): a checkpoint
    binding whose ordered digest chain is REORDERED must REJECT even when the
    attacker recomputes the chain digest — the ordered chain must be the
    bindings' own digests in the SAME positional order, and each binding's
    ``sequence`` must equal its position (1..N)."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                entries = binding["bindings"]
                # Swap the first and last bindings in place AND swap the ordered
                # digest list to match, AND recompute the chain digest — only
                # the positional/sequence binding can catch this re-combination.
                entries[0], entries[2] = entries[2], entries[0]
                binding["ordered_checkpoint_sha256"] = [
                    entries[0]["checkpoint_sha256"],
                    entries[1]["checkpoint_sha256"],
                    entries[2]["checkpoint_sha256"],
                ]
                binding["checkpoint_chain_sha256"] = gate._canonical_sha256(
                    binding["ordered_checkpoint_sha256"]
                )

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="reordered chain"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_checkpoint_binding_wrong_date() -> None:
    """C-122 supervision 18:13 counter-example (wrong date): a checkpoint
    binding whose carried dates DISAGREE with the dates embedded in the pair id
    itself must REJECT — the binding's dates must satisfy the canonical frozen
    time contract AND agree with the pair id's own dates."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                # Doctored dates that are STILL a canonical frozen pair (a
                # different valid pair's dates) — only the pair-id/date
                # agreement check can catch this swap.
                binding["bindings"][0]["departure_date"] = "2026-08-01"
                binding["bindings"][0]["return_date"] = "2026-08-06"

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError, match="dates disagree with the pair id"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_checkpoint_binding_wrong_request() -> None:
    """C-122 supervision 18:13 counter-example (wrong request): a checkpoint
    binding whose per-binding request identity is FOREIGN must REJECT — every
    binding must carry the SAME request SHA as the binding container, and the
    chain must be bound to one request identity."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                # A foreign request identity on one binding — the binding no
                # longer matches the container's request identity.
                binding["bindings"][1]["request_sha256"] = "e" * 64

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="request_sha256 does not match the binding's request identity",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_same_raw_copied_digest() -> None:
    """C-122 supervision 18:13 + round-19 gap-4 counter-example (same-raw
    self-consistent forgery): a compact that copies a producer's digests
    VERBATIM without the underlying content must REJECT — the validator
    RECOMPUTES every digest from the binding's carried fields, so a doctored
    business summary with a stale copied ``run_summary_sha256`` fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                # Doctor a business-summary field (source-task count) but keep
                # the run_summary digest AND the checkpoint digest as the
                # producer's verbatim values — only the full business-summary
                # recomputation can catch this forgery.
                binding["bindings"][0]["source_task_count"] = 1

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="run_summary_sha256 does not recompute",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_checkpoint_binding_foreign_api_payload() -> None:
    """C-122 round-19 (gap 4) counter-example (foreign request-payload binding):
    a compact whose carried ``api_payload_sha256`` does not match the checkpoint
    binding's request identity must REJECT — the checkpoint chain is bound to
    the raw API payload the run submitted, not merely a self-declared SHA."""

    compact = _layer6_compact_fixture()
    compact["api_payload_sha256"] = "e" * 64
    with pytest.raises(
        gate.GateStateChangedError,
        match="request_sha256 does not bind to the compact's api_payload_sha256",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_checkpoint_binding_non_completed_state() -> None:
    """C-122 round-19 (gap 4) counter-example (non-completed state): a
    checkpoint binding whose entry state is not ``completed`` must REJECT even
    when every digest is internally consistent — a failed/pending checkpoint
    cannot certify a passing gate."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                binding["bindings"][0]["state"] = "failed"

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="state 'failed' != 'completed'",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_checkpoint_binding_rejects_wrong_group_count() -> None:
    """C-122 round-19 (gap 4) counter-example (wrong group count): a checkpoint
    binding with FOUR bindings cannot be the frozen three-date-pair seal — the
    exact-count check rejects it even when the bindings cover a four-id pair
    set."""

    binding = _fixture_checkpoint_binding()
    binding["bindings"] = binding["bindings"] + [dict(binding["bindings"][-1])]
    pair_ids = [
        *_FIXTURE_PAIR_IDS,
        _frozen_v4_fixture_pair_id("2026-08-31", "2026-09-08"),
    ]
    with pytest.raises(
        gate.GateStateChangedError,
        match="has 4 bindings != the frozen scenario's exact 3 date pairs",
    ):
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            pair_ids,
        )


def test_verify_layer6_checkpoint_binding_rejects_passed_false() -> None:
    """C-122 supervision 03:46 (Block 3) counter-example: a checkpoint binding
    whose header records ``passed=false`` must REJECT even when the bindings
    list is a fully well-formed three-group chain — a non-passing seal cannot
    certify a passing gate."""

    binding = _fixture_checkpoint_binding()
    binding["passed"] = False
    with pytest.raises(
        gate.GateStateChangedError,
        match="checkpoint_binding passed False != true",
    ):
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            list(_FIXTURE_PAIR_IDS),
        )


def test_verify_layer6_checkpoint_binding_rejects_wrong_count_claim() -> None:
    """C-122 supervision 03:46 (Block 3) counter-example: a checkpoint binding
    whose header claims ``count=999`` must REJECT — the carried count must agree
    with the verified three-group chain, not be a self-declared number."""

    binding = _fixture_checkpoint_binding()
    binding["count"] = 999
    with pytest.raises(
        gate.GateStateChangedError,
        match="checkpoint_binding count 999 != the frozen scenario's exact 3 date pairs",
    ):
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            list(_FIXTURE_PAIR_IDS),
        )


@pytest.mark.parametrize(
    ("count_value", "expected_repr"),
    [
        (True, "True"),
        (3.0, "3.0"),
        ("3", "'3'"),
    ],
)
def test_verify_layer6_checkpoint_binding_rejects_non_integer_count(
    count_value: object, expected_repr: str
) -> None:
    """C-122 supervision 04:14 counter-example (strict type lock): ``count`` must
    be a STRICT JSON integer equal to 3 — ``True`` (bool, ``True == 1``), ``3.0``
    (float, ``3.0 != 3`` is False) and the string ``"3"`` must all REJECT even
    though each numerically equals / stringifies to three."""

    binding = _fixture_checkpoint_binding()
    binding["count"] = count_value
    expected = (
        rf"checkpoint_binding count {re.escape(expected_repr)} "
        r"!= the frozen scenario's exact 3 date pairs"
    )
    with pytest.raises(
        gate.GateStateChangedError,
        match=expected,
    ):
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            list(_FIXTURE_PAIR_IDS),
        )


@pytest.mark.parametrize(
    "passed_value",
    [1, 1.0, "true", True, False],
)
def test_verify_layer6_checkpoint_binding_rejects_non_strict_true_passed(
    passed_value: object,
) -> None:
    """C-122 supervision 04:14 counter-example (strict type lock): ``passed``
    must be the boolean singleton ``True`` — ``1``, ``1.0``, the string
    ``"true"`` and ``False`` all REJECT; only ``True`` (the control) passes."""

    binding = _fixture_checkpoint_binding()
    binding["passed"] = passed_value
    if passed_value is True:
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            list(_FIXTURE_PAIR_IDS),
        )
        return
    with pytest.raises(
        gate.GateStateChangedError,
        match=rf"checkpoint_binding passed {re.escape(repr(passed_value))} != true",
    ):
        gate._verify_layer6_checkpoint_binding(
            "done-gate-layer6-compact.json",
            "v4_source_graph",
            {"checkpoint_binding": binding},
            list(_FIXTURE_PAIR_IDS),
        )


def test_verify_layer6_compact_contract_rejects_checkpoint_binding_foreign_query_member() -> None:
    """C-122 round-19 (gap 4) counter-example (foreign query member): a
    checkpoint binding whose per-group query-task set contains a FOREIGN Source
    id must REJECT even when every digest is recomputed consistently — the
    per-group set must equal the canonical frozen graph exactly."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                binding = check["evidence"]["checkpoint_binding"]
                entry = binding["bindings"][0]
                entry["query_task_ids"] = sorted(
                    set(entry["query_task_ids"]) | {"source-ctrip-extra-forgery"}
                )
                entry["query_task_ids_sha256"] = gate._canonical_sha256(
                    entry["query_task_ids"]
                )
                from tripchord.agents.live_jobs import LivePlanningPairCheckpoint

                entry["checkpoint_sha256"] = LivePlanningPairCheckpoint._digest(
                    LivePlanningPairCheckpoint._checkpoint_summary(
                        {
                            "schema_version": "live-pair-checkpoint-v1",
                            "request_sha256": entry["request_sha256"],
                            "sequence": entry["sequence"],
                            "date_pair_id": entry["date_pair_id"],
                            "departure_date": entry["departure_date"],
                            "return_date": entry["return_date"],
                            "state": entry["state"],
                            "query_task_ids": entry["query_task_ids"],
                            "run_summary_sha256": entry["run_summary_sha256"],
                            "captured_at": entry["captured_at"],
                        }
                    )
                )
                binding["ordered_checkpoint_sha256"][0] = entry["checkpoint_sha256"]
                binding["checkpoint_chain_sha256"] = gate._canonical_sha256(
                    binding["ordered_checkpoint_sha256"]
                )

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="query_task_ids member set != the canonical frozen graph",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_frozen_v4_pair_id_generation_enforces_time_contract() -> None:
    """C-122 supervision 18:13: the canonical generation entry point enforces
    the time contract BEFORE digest generation — ``frozen_v4_pair_id`` raises
    ``ValueError`` for a 2030 / reversed / 1-night pair, and the acceptance
    predicate ``frozen_v4_pair_id_is_canonical`` rejects the same ids the
    producer and layer-6 validator rely on."""

    from tripchord.planning.frozen_graph import (
        frozen_v4_pair_id,
        frozen_v4_pair_id_is_canonical,
    )

    # The passing fixture pair (08-11→08-16, five nights) is canonical.
    passing = _frozen_v4_fixture_pair_id("2026-08-11", "2026-08-16")
    assert frozen_v4_pair_id_is_canonical(passing)

    assert frozen_v4_pair_id_is_canonical(
        _frozen_v4_fixture_pair_id("2030-01-01", "2030-01-06")
    ) is False
    assert frozen_v4_pair_id_is_canonical(
        _frozen_v4_fixture_pair_id("2026-08-20", "2026-08-15")
    ) is False
    for departure, return_date in (
        ("2026-08-11", "2026-08-12"),  # 1 night
        ("2026-08-11", "2026-08-20"),  # 9 nights
        ("2026-08-11", "2026-08-21"),  # 10 nights
    ):
        assert frozen_v4_pair_id_is_canonical(
            _frozen_v4_fixture_pair_id(departure, return_date)
        ) is False

    with pytest.raises(ValueError, match="after the frozen window's latest"):
        frozen_v4_pair_id(date(2030, 1, 1), date(2030, 1, 6))
    with pytest.raises(ValueError, match="not after departure"):
        frozen_v4_pair_id(date(2026, 8, 20), date(2026, 8, 15))
    with pytest.raises(ValueError, match="below the frozen scenario's minimum"):
        frozen_v4_pair_id(date(2026, 8, 11), date(2026, 8, 12))


def test_frozen_v4_pair_id_real_generation_path_enforces_time_contract() -> None:
    """C-122 supervision 02:56: the frozen time contract is wired into the REAL
    generation path — ``FlexibleDateExplorer._pair_id`` (the method production
    exploration calls) must delegate to ``frozen_v4_pair_id`` for the frozen
    scenario window and RAISE on an out-of-contract pair BEFORE a digest is ever
    produced.  A 2030 departure / reversed dates / 1/9/10-night pair rejected
    only at acceptance side is the gap this closes: the same counter-examples
    must fail at GENERATION time too."""

    from datetime import date

    from tripchord.planning.flexible_dates import FlexibleDateExplorer
    from tripchord.planning.frozen_graph import _FROZEN_V4_TRAVEL_WINDOW

    explorer = FlexibleDateExplorer()
    frozen_window = _FROZEN_V4_TRAVEL_WINDOW

    # An in-contract pair routes through the canonical helper and yields the
    # canonical fixture id (the digest is identical to the generic derivation).
    assert (
        explorer._pair_id(frozen_window, date(2026, 8, 11), date(2026, 8, 16))
        == _frozen_v4_fixture_pair_id("2026-08-11", "2026-08-16")
    )

    # Out-of-contract pairs RAISE at generation time (ValueError, before any
    # digest is returned) — the real path, not the standalone helper.
    with pytest.raises(ValueError, match="after the frozen window's latest"):
        explorer._pair_id(frozen_window, date(2030, 1, 1), date(2030, 1, 6))
    with pytest.raises(ValueError, match="not after departure"):
        explorer._pair_id(frozen_window, date(2026, 8, 20), date(2026, 8, 15))
    with pytest.raises(ValueError, match="below the frozen scenario's minimum"):
        explorer._pair_id(frozen_window, date(2026, 8, 11), date(2026, 8, 12))
    with pytest.raises(ValueError, match="above the frozen scenario's maximum"):
        explorer._pair_id(frozen_window, date(2026, 8, 11), date(2026, 8, 20))
    with pytest.raises(ValueError, match="above the frozen scenario's maximum"):
        explorer._pair_id(frozen_window, date(2026, 8, 11), date(2026, 8, 21))

    # The routing is precise: a FOREIGN window (different destination) keeps the
    # generic generation path and must NOT inherit the frozen contract — a 2030
    # pair for a non-frozen scenario is generated, not rejected.
    foreign_window = frozen_window.model_copy(
        update={"destination": "普吉岛", "destination_code": "HKT"}
    )
    foreign_id = explorer._pair_id(
        foreign_window, date(2030, 1, 1), date(2030, 1, 6)
    )
    assert foreign_id.startswith("date-pair:2030-01-01:2030-01-06:")


def test_verify_layer6_compact_contract_rejects_wrong_pair_swap() -> None:
    """C-122 supervision 01:10 counter-example (pair swap): a v4 source graph
    whose pair_ids keep the frozen count but SWAP one of the run's actual sealed
    pairs for a different well-formed canonical pair (digest recomputes, but the
    id is not one the run sealed) must REJECT — the pair-id SET must equal the
    run's checkpoint-bound sealed pair ids exactly."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                swapped = _frozen_v4_fixture_pair_id("2026-08-01", "2026-08-06")
                check["evidence"]["pair_ids"] = [  # type: ignore[index]
                    swapped,
                    _FIXTURE_PAIR_IDS[1],
                    _FIXTURE_PAIR_IDS[2],
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="pair_ids set != the run's checkpoint-bound sealed pair set",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_checkpoint_pair() -> None:
    """C-122 supervision 01:10 counter-example (missing pair): a v4 source graph
    whose pair_ids carry the full 3-id canonical set but whose checkpoint-bound
    record covers only 2 pairs must REJECT — the compact's pair set must be
    covered EXACTLY by the run's checkpoint-bound sealed pair ids."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["checkpoint_bound_pair_ids"] = list(  # type: ignore[index]
                    _FIXTURE_PAIR_IDS[:2]
                )

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="checkpoint_bound_pair_ids does not cover exactly the frozen pair set",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_extra_pair_id() -> None:
    """C-122 supervision 01:10 counter-example (extra pair): a v4 source graph
    carrying FOUR well-formed canonical pair ids must REJECT — the frozen
    scenario seals exactly three date pairs, so an extra pair is a forged graph."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                extra = _frozen_v4_fixture_pair_id("2026-08-02", "2026-08-07")
                check["evidence"]["pair_ids"] = [  # type: ignore[index]
                    *list(_FIXTURE_PAIR_IDS),
                    extra,
                ]
                check["evidence"]["checkpoint_bound_pair_ids"] = [  # type: ignore[index]
                    *list(_FIXTURE_PAIR_IDS),
                    extra,
                ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="pair_ids != the frozen scenario's exact 3 date pairs",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_count_not_bound_to_id_sets() -> None:
    """C-122 round-18 supervision 16:03 counter-example B: a v4 source graph whose
    declared per-pair task count is not bound to the actual Source-id / query-shape
    sets (expected_tasks=13 but only 1 Source id and 1 query shape) must REJECT —
    the count must equal the ID-set lengths, one Source id / query shape per task."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_browser_source_ids"] = ["source-ctrip-flight"]  # type: ignore[index]
                check["evidence"]["expected_query_shapes"] = ["ctrip:flight"]  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="expected_browser_source_ids length",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_query_shapes_not_bound_to_count() -> None:
    """C-122 round-18 supervision 16:03 counter-example B (query-shape side): a
    v4 source graph carrying the frozen Source-id set but only 1 query shape must
    REJECT — the query-shape set must also be one-per-task."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_query_shapes"] = ["ctrip:flight"]  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="expected_query_shapes length",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_5_tasks_1_id_each() -> None:
    """C-122 round-18 supervision 16:03 counter-example B (exact form): a v4
    source graph that declares expected_tasks=5 while its Source-id / query-shape
    sets each carry only 1 entry must REJECT — a count that the ID sets do not
    actually realize is a forged graph."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["expected_browser_tasks_per_pair"] = 5  # type: ignore[index]
                check["evidence"]["expected_browser_source_ids"] = ["source-ctrip-flight"]  # type: ignore[index]
                check["evidence"]["expected_query_shapes"] = ["ctrip:flight"]  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="frozen scenario's exact per-pair browser task count",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_per_pair_breakdown() -> None:
    """C-122 HG-G counter-example: dropping the per-pair breakdown from a passing
    v4 source graph must fail closed — the frozen-scenario per-pair contract is
    required evidence, not an optional binding."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"].pop("per_pair", None)  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="per_pair"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_total_not_equal_pair_sum() -> None:
    """C-122 HG-G counter-example: a v4 source graph whose declared total does not
    equal the sum of the per-pair query-task counts must fail closed — the total
    must be recomputable from the per-pair breakdown (13+13+13=39 here)."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                check["evidence"]["total_planned_task_count"] = 14  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError, match="per-pair query-task sum"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_foreign_browser_source_member() -> None:
    """C-122 round-19 (supervision 17:03 Block 1 counter-example): a v4 source
    graph with the RIGHT count (13 unique browser Source ids) but a FOREIGN member
    (one canonical id replaced by an id outside the frozen graph) must REJECT —
    the member SET must equal the canonical frozen graph exactly, never just have
    the right length."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                source_ids = check["evidence"]["expected_browser_source_ids"]  # type: ignore[index]
                assert "source-tongcheng-flight" in source_ids
                source_ids[source_ids.index("source-tongcheng-flight")] = (  # type: ignore[index]
                    "source-fliggy-flight"
                )

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="member set != the canonical frozen graph browser Source-id set",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_wrong_pair_member_swap() -> None:
    """C-122 round-19 (supervision 17:03 Block 1 counter-example): a v4 source
    graph whose TOP-LEVEL sets are canonical but whose per-pair breakdown swaps
    pair 2's query-shape members to a foreign set (13 unique shapes, but not the
    canonical set) must REJECT — every pair's member sets must equal the frozen
    graph exactly (wrong-pair-swap gate)."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                for entry in check["evidence"]["per_pair"]:  # type: ignore[index]
                    if entry["pair_id"] == _FIXTURE_PAIR_IDS[1]:
                        shapes = sorted(gate._V4_FROZEN_QUERY_SHAPES)
                        assert "tongcheng:flight" in shapes
                        shapes[shapes.index("tongcheng:flight")] = "fliggy:flight"
                        entry["query_task_ids"] = shapes

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="query shape member set != the canonical frozen graph set",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_missing_icom_task() -> None:
    """C-122 round-19 (supervision 17:03 Block 1 counter-example): a v4 source
    graph whose iCom task set DROPS a canonical member (4 -> 3, still unique and
    positive) must REJECT — the iCom task set must equal the frozen graph exactly."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                icom_ids = check["evidence"]["expected_icom_task_ids"]  # type: ignore[index]
                icom_ids.pop(0)

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="expected_icom_task_ids member set != the canonical frozen graph iCom task-id set",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_extra_icom_task_member() -> None:
    """C-122 round-19 (supervision 17:03 Block 1 counter-example): a v4 source
    graph whose per-pair iCom member list contains a FOREIGN extra member (size 5
    where the frozen graph seals 4) must REJECT even though the top-level iCom set
    and every count stays canonical — a pair carrying an extra iCom task is a
    forged graph."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "v4_source_graph":
                for entry in check["evidence"]["per_pair"]:  # type: ignore[index]
                    if entry["pair_id"] == _FIXTURE_PAIR_IDS[0]:
                        entry["icom_source_task_ids"] = [
                            *sorted(gate._V4_FROZEN_ICOM_TASK_IDS),
                            "public-transfer-icom-evil-extra",
                        ]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError,
        match="iCom task id member list is missing or has the wrong size",
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_coverage_mode_degraded() -> None:
    """C-122 HG-G counter-example (supervision real-run): a strict-coverage check
    that records coverage_mode=degraded must REJECT — the strict coverage receipt
    only ever carries coverage_mode=strict."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "strict_selected_plan_platform_coverage":
                check["evidence"]["coverage_mode"] = "degraded"  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="!= 'strict'"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_wrong_stage_counts() -> None:
    """C-122 round-18 gate-2 counter-example: a stage contract that does not seal
    exactly three explorations and two publication refreshes fails closed — the
    fixed stage contract is a semantic invariant, not a record count."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "stage_aware_exploration_publication_contract":
                check["evidence"]["exploration_count"] = 2  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(
        gate.GateStateChangedError, match="seal exactly 3 explorations"
    ):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_source_snapshot_mismatch() -> None:
    """C-122 round-18 gate-2 counter-example: snapshot_count differing from
    source_task_count breaks the evidence chain and fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "real_v4_browser_source_evidence":
                check["evidence"]["snapshot_count"] = 4  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="snapshot_count"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_insufficient_overlap_intervals() -> None:
    """C-122 round-18 gate-2 counter-example: an overlap proof with fewer than
    three time intervals cannot prove real concurrency and fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "observed_cross_platform_overlap":
                check["evidence"]["interval_count"] = 2  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="3-provider concurrent"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_single_provider_overlap() -> None:
    """C-122 round-18 gate-2 counter-example: an overlap proof whose maximum
    distinct concurrent providers is not exactly three (a single-provider overlap
    masquerading as cross-platform) fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "observed_cross_platform_overlap":
                check["evidence"]["max_overlapping_providers"] = 1  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="max_providers"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_partial_platform_set() -> None:
    """C-122 round-18 gate-2 counter-example: strict coverage naming a partial or
    foreign provider set (missing a platform, or adding an unknown one) fails
    closed — the completion receipt must name exactly the fixed three OTA
    platforms."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "strict_selected_plan_platform_coverage":
                check["evidence"]["providers"] = ["ctrip", "qunar"]  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="fixed three-OTA"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_icom_coverage_not_passed() -> None:
    """C-122 round-18 gate-2 counter-example: icom exploration coverage recorded
    as ``{"passed": false}`` cannot back a passing check and fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "icom_exploration_and_publication_evidence":
                check["evidence"]["exploration_full_coverage"]["passed"] = False  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="not passed"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


def test_verify_layer6_compact_contract_rejects_empty_publication_targets() -> None:
    """C-122 round-18 gate-2 counter-example: an icom check with no publication
    target task ids cannot bind the publication refresh and fails closed."""

    def mutate(checks: Any) -> None:
        for check in checks:
            if check["name"] == "icom_exploration_and_publication_evidence":
                check["evidence"]["publication_target_task_ids"] = []  # type: ignore[index]

    compact = _layer6_compact_with_evidence_mutated(mutate)
    with pytest.raises(gate.GateStateChangedError, match="is empty"):
        gate._verify_layer6_compact_contract(
            "done-gate-layer6-compact.json",
            compact,
            tested_commit_sha="a" * 40,
        )


# --- compact -> E -> P blob readback counter-examples -------------------------


def _commit_compact_to_evidence_commit(
    monkeypatch: pytest.MonkeyPatch,
    clean_repo: Path,
    staging_dir: Path,
    compact_factory: Any,
) -> str:
    """Commit a crafted layer-6 compact blob into evidence commit E (HEAD) with a
    manifest binding it as committed, then re-read it exactly as the post-commit
    gate does.  ``compact_factory(tested_sha)`` builds the compact — a semantic
    counterexample keeps repo==runtime==S and fails ONLY on its violation.
    Returns E's sha."""
    _patch_root(monkeypatch, clean_repo)
    _populate_full_required_evidence(monkeypatch, staging_dir)
    tested_sha = _head(clean_repo)
    compact = compact_factory(tested_sha)
    payload = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    staged_compact = staging_dir / gate._COMPACT_E2E_STAGED_NAME
    staged_compact.write_text(payload, encoding="utf-8")
    manifest = {
        "schema_version": gate._MANIFEST_SCHEMA,
        "tested_commit_sha": tested_sha,
        "run_id": "test-run",
        "evidence_commit": tested_sha,
        "generated_at": "2026-08-10T00:00:00+00:00",
        "branch": "main",
        "files": gate._manifest_files(staging_dir),
        "layer_verdicts": {"5_real_canary": {}, "6_full_e2e": {}},
    }
    results = clean_repo / "benchmarks" / "results"
    results.mkdir(parents=True, exist_ok=True)
    for staged_name, tracked_rel in gate._EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if staged.is_file():
            (results / Path(tracked_rel).name).write_bytes(staged.read_bytes())
    (results / Path(gate._MANIFEST_REL).name).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    gate._git("add", "--", str(results), check=True)
    gate._git("commit", "-q", "-m", "crafted compact evidence", check=True)
    gate._verify_evidence_contract(
        _head(clean_repo), staging_dir, tested_commit_sha=tested_sha, run_id="test-run"
    )
    return _head(clean_repo)


def test_blob_readback_rejects_semantic_violation(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 acceptance blob readback counter-example: the compact committed in E,
    when re-read from the blob at the phase-closed gate, rejects a check whose
    evidence is semantically inconsistent with its passing verdict."""

    def build(tested_sha: str) -> dict[str, object]:
        compact = _layer6_compact_fixture()
        compact["repo_revision"]["commit_sha"] = tested_sha  # type: ignore[index]
        compact["runtime_before_run"]["runtime_provenance"]["commit_sha"] = tested_sha  # type: ignore[index]
        for check in compact["done_gate"]["checks"]:  # type: ignore[index]
            if check["name"] == "exact_budget_and_selected_evidence":
                check["evidence"]["computed_total_cents"] = 1  # type: ignore[index]
        return compact

    with pytest.raises(gate.GateStateChangedError, match="computed_total_cents"):
        _commit_compact_to_evidence_commit(
            monkeypatch, clean_repo, staging_dir, build
        )


def test_blob_readback_rejects_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_repo: Path, staging_dir: Path
) -> None:
    """C-122 acceptance blob readback counter-example: the compact committed in E
    whose repo revision names a different commit than the run's tested SHA fails
    closed when re-read from the blob."""

    def build(tested_sha: str) -> dict[str, object]:
        compact = _layer6_compact_fixture()
        compact["repo_revision"]["commit_sha"] = "b" * 40  # type: ignore[index]
        compact["runtime_before_run"]["runtime_provenance"]["commit_sha"] = "b" * 40  # type: ignore[index]
        return compact

    with pytest.raises(gate.GateStateChangedError, match="!= tested_commit_sha"):
        _commit_compact_to_evidence_commit(
            monkeypatch, clean_repo, staging_dir, build
        )


def test_verify_layer5_compact_contract_rejects_empty_scope_evidence() -> None:
    """C-122 Fix 3 counter-example: a layer-5 compact whose scope is reduced to a
    bare verdict (no per-scope evidence binding) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:flight",
        "kind": "companion_heartbeat",
        "provider": "ctrip",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="no per-scope evidence binding"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_valid_seven_plus_none() -> None:
    """C-122 Fix 6 counter-example: the valid certified canary scopes plus one
    extra ``None`` entry must fail closed — the array length must equal the
    certified scope set size."""
    compact = _layer5_compact_fixture()
    expected = sorted(gate._ALL_CERTIFIED_CANARY_SCOPES)
    compact["coverage"] = {  # type: ignore[assignment]
        "expected_scope_count": len(expected),
        "expected_scopes": expected,
        "observed_scope_count": len(expected),
        "passed_scope_count": len(expected),
        "missing": [],
    }
    compact["scopes"] = compact["scopes"] + [None]  # type: ignore[list-item, operator]
    with pytest.raises(
        gate.GateStateChangedError,
        match=f"scope list count != {len(expected)}",
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_duplicate_scope() -> None:
    """C-122 Fix 6 counter-example: a scope name repeated in the input array
    fails closed — six entries must be six distinct scopes."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    duplicate = dict(scopes[0])  # type: ignore[arg-type]
    scopes[1] = duplicate  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="scope names must be unique"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_malformed_entry() -> None:
    """C-122 Fix 6 counter-example: a malformed (non-object) scope entry fails
    closed instead of being skipped."""
    compact = _layer5_compact_fixture()
    compact["scopes"][0] = "ctrip:flight"  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="malformed scope entry"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_missing_scope_name() -> None:
    """C-122 Fix 6 counter-example: an object with no scope name is malformed
    and fails closed."""
    compact = _layer5_compact_fixture()
    entry = dict(compact["scopes"][0])  # type: ignore[arg-type]
    del entry["scope"]  # type: ignore[index]
    compact["scopes"][0] = entry  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="missing or invalid scope name"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_browser_scope_without_companion() -> None:
    """C-122 Fix 6 counter-example: a browser scope whose evidence carries no
    Companion identity (per-item authentication) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:flight",
        "kind": "companion_heartbeat",
        "provider": "ctrip",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {"authorized_scope_keys": ["ctrip:flight"]},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="no Companion identity"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_unauthorized_browser_scope() -> None:
    """C-122 Fix 6 counter-example: a browser scope whose Companion does not
    authorize this exact scope key (per-item authentication) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:flight",
        "kind": "companion_heartbeat",
        "provider": "ctrip",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {
            "companion_id": "comp-1",
            "authorized_scope_keys": ["qunar:flight"],
        },
    }
    with pytest.raises(
        gate.GateStateChangedError, match="not authorized by its Companion evidence"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_icom_without_query_sample() -> None:
    """C-122 Fix 6 counter-example: the icom scope whose evidence carries no
    read-only query sample (per-item authentication) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    icom_index = next(
        i
        for i, s in enumerate(scopes)
        if isinstance(s, dict) and s.get("scope") == "icom:transfer"
    )
    scopes[icom_index] = {  # type: ignore[index]
        "scope": "icom:transfer",
        "kind": "icom_public_api",
        "provider": "icom",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {"options": 3, "source_url_count": 3},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="sample must carry service_name"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_scope_without_kind() -> None:
    """C-122 round-18 gate-1 counter-example: a scope entry with no canary
    ``kind`` (forged/missing canary origin) fails closed — the kind must be the
    one the certified canary actually produced."""
    compact = _layer5_compact_fixture()
    entry = dict(compact["scopes"][0])  # type: ignore[arg-type]
    del entry["kind"]  # type: ignore[index]
    compact["scopes"][0] = entry  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="carries no canary kind"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_scope_without_provider() -> None:
    """C-122 round-18 gate-1 counter-example: a scope entry with no ``provider``
    (missing canary kind origin) fails closed — every scope must name the real
    platform that produced it."""
    compact = _layer5_compact_fixture()
    entry = dict(compact["scopes"][0])  # type: ignore[arg-type]
    del entry["provider"]  # type: ignore[index]
    compact["scopes"][0] = entry  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="provider None != expected 'ctrip'"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_wrong_provider() -> None:
    """C-122 round-18 gate-1 counter-example: a scope whose provider does not
    match its platform prefix fails closed — a qunar scope cannot claim it came
    from ctrip."""
    compact = _layer5_compact_fixture()
    entry = dict(compact["scopes"][0])  # type: ignore[arg-type]
    entry["provider"] = "qunar"  # type: ignore[index]
    compact["scopes"][0] = entry  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="provider 'qunar' != expected 'ctrip'"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_icom_zero_options() -> None:
    """C-122 round-18 gate-1 counter-example: a passing icom scope whose evidence
    carries no positive option count (0 / negative / non-int) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    icom_index = next(
        i
        for i, s in enumerate(scopes)
        if isinstance(s, dict) and s.get("scope") == "icom:transfer"
    )
    scopes[icom_index] = {  # type: ignore[index]
        "scope": "icom:transfer",
        "kind": "icom_public_api",
        "provider": "icom",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {"options": 0, "sample": _per_scope_canary_evidence("icom:transfer")["sample"]},
    }
    with pytest.raises(
        gate.GateStateChangedError, match="no positive option count"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_icom_sample_missing_quote() -> None:
    """C-122 round-18 gate-1 counter-example: an icom sample reduced to a
    service name alone (no price / currency / departure time) is not a real
    quote and fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    icom_index = next(
        i
        for i, s in enumerate(scopes)
        if isinstance(s, dict) and s.get("scope") == "icom:transfer"
    )
    scopes[icom_index] = {  # type: ignore[index]
        "scope": "icom:transfer",
        "kind": "icom_public_api",
        "provider": "icom",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {
            "options": 3,
            "sample": {"service_name": "speed-boat"},
        },
    }
    with pytest.raises(
        gate.GateStateChangedError, match="sample must carry service_name"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_browser_scope_foreign_companion() -> None:
    """C-122 round-18 gate-1 counter-example: a browser scope bound to a
    Companion the compact's own companion_status never lists fails closed — the
    scope may not name a fresh Companion that is not connected."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:flight",
        "kind": "companion_heartbeat",
        "provider": "ctrip",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {
            "companion_id": "foreign-comp",
            "authorized_scope_keys": ["ctrip:flight"],
            "adapter_version": "test-adapter",
        },
    }
    with pytest.raises(
        gate.GateStateChangedError, match="not in the connected companion_status"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_browser_scope_no_receipt() -> None:
    """C-122 round-18 gate-1 counter-example: a browser scope whose Companion
    evidence names an identity but carries no heartbeat receipt
    (adapter_version / contract_version / runtime_instance_id) fails closed."""
    compact = _layer5_compact_fixture()
    scopes = compact["scopes"]  # type: ignore[assignment]
    scopes[0] = {  # type: ignore[index]
        "scope": "ctrip:flight",
        "kind": "companion_heartbeat",
        "provider": "ctrip",
        "passed": True,
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": {
            "companion_id": "comp-1",
            "authorized_scope_keys": ["ctrip:flight"],
        },
    }
    with pytest.raises(
        gate.GateStateChangedError, match="no heartbeat receipt"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_verify_layer5_compact_contract_rejects_build_sha_not_strict_64hex() -> None:
    """C-122 round-18 gate-1 counter-example: a Companion build fingerprint that
    is not an exact 64-hex sha256 (32-hex, 48-hex, uppercase hex all rejected)
    fails closed — the build binding must be the precise digest."""
    compact = _layer5_compact_fixture()
    companion = compact["companion_status"]["companions"][0]  # type: ignore[index]
    companion["build_sha256"] = "a" * 32  # type: ignore[index]
    with pytest.raises(
        gate.GateStateChangedError, match="64-hex build_sha256"
    ):
        gate._verify_layer5_compact_contract(
            "done-gate-layer5-compact.json", compact
        )


def test_desensitize_check_value_redacts_tokens_keeps_bindings() -> None:
    """C-122 acceptance: the schema-driven desensitizer redacts URL-bearing and
    token-like strings AND any 64-hex in a non-hash position, keeps explicit
    content-addressable hash bindings, and drops unknown nested keys — never a
    recursive copy of the raw evidence dict."""
    schema = {
        "count": gate._EVIDENCE_SCALAR,
        "provider": gate._EVIDENCE_SCALAR,
        "candidate_set_sha256": gate._EVIDENCE_HASH,
        "source_url": gate._EVIDENCE_SCALAR,
        "bearer_token": gate._EVIDENCE_SCALAR,
        "unknown_field": gate._EVIDENCE_SCALAR,
        "nested": {
            "pair_id": gate._EVIDENCE_SCALAR,
            "raw": gate._EVIDENCE_SCALAR,
            "secret_hex": gate._EVIDENCE_SCALAR,
        },
    }
    evidence = {
        "count": 3,
        "provider": "ctrip",
        "candidate_set_sha256": "a" * 64,
        "source_url": "https://flights.ctrip.com/booking?dep=BJS&arr=SHA",
        "bearer_token": (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        ),
        "sneaky_top_level": {"recursive": "copy"},
        "nested": {
            "pair_id": "pair-1",
            "raw": "http://127.0.0.1:8000/api/v1/x",
            "secret_hex": "c" * 64,
            "sneaky": {"also": "dropped"},
        },
    }
    redacted = gate._desensitize_check_value(evidence, schema)
    assert redacted["count"] == 3
    assert redacted["provider"] == "ctrip"
    assert redacted["candidate_set_sha256"] == "a" * 64
    assert redacted["nested"]["pair_id"] == "pair-1"
    assert redacted["source_url"].startswith("url#")
    assert redacted["bearer_token"].startswith("secret#")
    assert redacted["nested"]["raw"].startswith("url#")
    assert redacted["nested"]["secret_hex"].startswith("secret#")
    # Unknown keys are never recursively copied into the compact.
    assert "sneaky_top_level" not in redacted
    assert "sneaky" not in redacted["nested"]


def test_desensitize_check_value_hex_only_survives_hash_position() -> None:
    """C-122 acceptance: a 64-hex string is only a safe content-addressable
    binding inside an explicit ``_EVIDENCE_HASH`` field; in an arbitrary scalar
    position it is redacted as a token-shaped secret."""
    hash_schema = {"candidate_set_sha256": gate._EVIDENCE_HASH}
    scalar_schema = {"some_token": gate._EVIDENCE_SCALAR}
    kept = gate._desensitize_check_value(
        {"candidate_set_sha256": "a" * 64}, hash_schema
    )
    assert kept["candidate_set_sha256"] == "a" * 64
    redacted = gate._desensitize_check_value({"some_token": "a" * 64}, scalar_schema)
    assert redacted["some_token"].startswith("secret#")


def test_desensitized_check_evidence_injects_candidate_sha() -> None:
    """C-122 Fix 3: the compact builder injects the recomputable candidate-set
    SHA binding for the prefrozen-candidate check from the report top-level."""
    candidate_sha = "a" * 64
    raw_check = {
        "name": "prefrozen_stay_plan_candidate_set",
        "passed": True,
        "summary": "ok",
        "evidence_refs": [f"sha256:{candidate_sha}"],
    }
    payload = {"api_payload_candidate_set_sha256": candidate_sha}
    evidence = gate._desensitized_check_evidence(raw_check, payload)
    assert evidence["candidate_set_sha256"] == candidate_sha
    assert evidence["evidence_refs"] == [f"sha256:{candidate_sha}"]


def test_desensitized_check_evidence_never_empty() -> None:
    """C-122 Fix 3: a check with neither evidence nor refs still carries a
    non-empty structural item so the committed trail is never a bare verdict."""
    evidence = gate._desensitized_check_evidence(
        {"name": "v4_source_graph", "passed": True, "summary": "ok"}, {}
    )
    assert isinstance(evidence, dict)
    assert evidence


def test_compact_live_e2e_preserves_per_check_evidence(tmp_path: Path) -> None:
    """C-122 Fix 3 end-to-end: the layer-6 compact builder preserves the
    desensitized per-check structured evidence and injects the candidate-set SHA
    binding, so the compact is never a bare 15-boolean verdict list."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "live-done-gate-v4.json").write_text(
        json.dumps(_realistic_e2e_evidence()), encoding="utf-8"
    )
    compact = gate._compact_live_e2e(staging)
    assert compact is not None
    assert compact["done_gate"]["check_count"] == 15
    checks_by_name = {
        check["name"]: check for check in compact["done_gate"]["checks"]
    }
    for check in compact["done_gate"]["checks"]:
        assert isinstance(check.get("evidence"), dict) and check["evidence"]
    prefrozen = checks_by_name["prefrozen_stay_plan_candidate_set"]
    assert prefrozen["evidence"]["candidate_set_sha256"] == "a" * 64
    strict = checks_by_name["strict_selected_plan_platform_coverage"]
    assert strict["evidence"]["providers"] == ["ctrip", "qunar", "tongcheng"]
    # C-122 round-18 HG-E: the whitelist must NOT drop the six new recomputable
    # bindings — the compact carries them through desensitization.
    v4 = checks_by_name["v4_source_graph"]["evidence"]
    assert v4["expected_query_shapes"]
    assert v4["expected_icom_task_ids"]
    assert v4["pair_ids"]
    # C-122 supervision 01:10: the compact merges the job control plane's
    # checkpoint-bound sealed pair ids into the v4_source_graph evidence — an
    # independent record the validator cross-checks against the producer's own
    # pair_ids (rejects foreign / swapped / missing / extra pairs).
    assert v4["checkpoint_bound_pair_ids"] == list(_FIXTURE_PAIR_IDS)
    assert v4["total_planned_task_count"] > 0
    assert strict["evidence"]["coverage_mode"] == "strict"
    assert strict["evidence"]["all_platforms_complete"] is True
    assert compact["companion_preflight"]["stale_after_seconds"] == 45
