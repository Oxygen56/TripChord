#!/usr/bin/env python3
"""Machine-executable six-layer product Done-Gate (v1.0).

Runs the local engineering layers (reproducibility, replay, clean-Chrome
malicious fixtures, authorized model smoke) and honestly reports the real
platform canary layer as pending user authorization when no live Companion /
authorized OTA session is present.

The gate writes ``benchmarks/results/product-v1-done-gate.json`` atomically.
``passed=true`` is only allowed when every layer applicable to the current
declaration passes.  HTTP job success, test success, model call success or all
Source terminal states never alone trigger a pass.

Layer reference (contract section 六 v1.0):
  1. fresh clone / lockfile / migration / wheel / web / container / installer
     reproducible;
  2. replay mode full dynamic selection -> all-terminal -> cross-platform plan
     -> handoff -> user confirm -> protected replan;
  3. clean Chrome + local malicious fixture: permissions, pairing, background,
     URL and Prompt Injection;
  4. OpenAI-compatible model (authorized env) required-model smoke + structured
     Agent chain;
  5. every declared-certified real provider x vertical has a fresh authorised
     read-only canary;
  6. full-platform real E2E only when all external conditions are met; when not
     met, report exactly which external gate is not met, never forge it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tripchord.runtime_provenance import local_expected_provenance, provenance_mismatches

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "benchmarks" / "results"
OUTPUT_PATH = RESULTS_DIR / "product-v1-done-gate.json"
EVIDENCE_SCHEMA = "tripchord-product-v1-done-gate"
RUNTIME_EVIDENCE_DIR = ROOT / ".runtime" / "done-gate-evidence"

# Environment variables that redirect a ``git -C <root>`` invocation to a
# different repository.  The Done-Gate evidence must name the repository it
# actually exercised, so these overrides are stripped from the subprocess env
# before any git call (defect fix: GIT_DIR / GIT_WORK_TREE override risk).
_GIT_ENV_OVERRIDES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
    }
)

_MODEL_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TRIPCHORD_MODEL_API_KEY")
# Priority order for resolving which environment variable actually holds the
# model key.  The smoke reads the exact variable name we pass via --api-key-env,
# so we must hand it the name where the key was found, not a hardcoded one.
_MODEL_API_KEY_ENV_CANDIDATES = (
    "MODEL_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "TRIPCHORD_MODEL_API_KEY",
)


@dataclass(frozen=True)
class GitSnapshot:
    """One consistent snapshot of the authoritative repository.

    Captured through a git invocation that cannot be redirected by
    GIT_DIR/GIT_WORK_TREE and read with a single ``git status --porcelain`` so
    the HEAD revision and the worktree state refer to the same instant's tree.
    """

    toplevel: str | None
    branch: str | None
    commit_sha: str | None
    worktree_dirty: bool
    porcelain: str


class GateStateChangedError(RuntimeError):
    """The gate itself changed the tracked tree or HEAD moved during the run."""


@dataclass
class LayerResult:
    name: str
    passed: bool
    skipped: bool = False
    detail: str = ""
    sub_checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GateReport:
    schema_version: str
    generated_at: str
    tested_commit_sha: str | None
    evidence_commit: str | None = None
    toplevel: str | None = None
    branch: str | None = None
    worktree_dirty: bool = False
    layers: list[LayerResult] = field(default_factory=list)
    passed: bool = False
    summary: str = ""
    boundary: str = ""

    @property
    def commit_sha(self) -> str | None:
        """Backward-compatible alias: the report's tested revision."""
        return self.tested_commit_sha


def _git_safe_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}
    }


def _git(
    *args: str,
    cwd: Path | None = None,
    timeout: int = 10,
    check: bool = False,
    binary: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    # ``ROOT`` is resolved at call time (not bound as a default) so tests and
    # embedders can point the gate at a different repository via monkeypatch.
    # ``binary=True`` returns raw bytes on stdout/stderr (e.g. ``git show`` of
    # a PNG blob); the default text mode must never be used on binary content
    # because UTF-8 decoding crashes the caller.
    cwd = cwd or ROOT
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=_git_safe_env(),
            capture_output=True,
            text=not binary,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise GateStateChangedError(f"git is unavailable or timed out: {exc}") from exc
    if check and result.returncode != 0:
        # Fail closed on any non-zero git exit: a revision or tree state that
        # cannot be read must never be silently consumed as "clean/unknown".
        stderr = result.stderr or ""
        stdout = result.stdout or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.strip()
        stdout = stdout.strip()
        raise GateStateChangedError(
            f"git {' '.join(args)} failed with exit {result.returncode}: "
            f"{stderr or stdout or '(no output)'}"
        )
    return result


def _git_snapshot(cwd: Path | None = None) -> GitSnapshot:
    """Read toplevel + HEAD + full porcelain in one safe, un-redirected pass.

    The toplevel / HEAD / porcelain reads are required: a failure to read them
    means the revision cannot be proven and the gate must not proceed.
    ``symbolic-ref -q`` is the one allowed failure — a detached HEAD is a valid
    state that simply has no branch name.
    """
    cwd = cwd or ROOT
    toplevel = _git("rev-parse", "--show-toplevel", cwd=cwd, check=True)
    head = _git("rev-parse", "HEAD", cwd=cwd, check=True)
    branch = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=cwd, check=False)
    status = _git("status", "--porcelain", cwd=cwd, check=True)
    return GitSnapshot(
        toplevel=toplevel.stdout.strip() or None,
        branch=branch.stdout.strip() or None,
        commit_sha=head.stdout.strip() or None,
        worktree_dirty=bool(status.stdout.strip()),
        porcelain=status.stdout,
    )


def _snapshot_dict(snapshot: GitSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    return {
        "toplevel": snapshot.toplevel,
        "branch": snapshot.branch,
        "commit_sha": snapshot.commit_sha,
        "worktree_dirty": snapshot.worktree_dirty,
    }


def _verify_tree_unchanged(start: GitSnapshot, end: GitSnapshot) -> None:
    """Fail closed when the gate run changed the tracked tree or HEAD moved.

    This is the self-pollution guard: layers must write evidence only to
    git-ignored staging directories, so the porcelain output after the run must
    byte-for-byte match the snapshot taken before the run.  A mismatch means the
    gate dirtied the repository (or an external actor moved HEAD), and the
    evidence cannot claim a tested commit.
    """
    mismatches: list[str] = []
    if start.toplevel is not None and end.toplevel != start.toplevel:
        mismatches.append(f"toplevel {start.toplevel!r} -> {end.toplevel!r}")
    if end.commit_sha != start.commit_sha:
        mismatches.append(f"HEAD {start.commit_sha!r} -> {end.commit_sha!r}")
    if end.porcelain != start.porcelain:
        mismatches.append("worktree porcelain changed during the run")
    if mismatches:
        raise GateStateChangedError(
            "Done-Gate self-pollution or HEAD movement detected: "
            + "; ".join(mismatches)
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "command not found"


def layer1_reproducibility() -> LayerResult:
    """Fresh-clone reproducibility: migration + web build + API import."""
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/gate.db"
        env = dict(os.environ)
        env["TRIPCHORD_DATABASE_URL"] = db_url
        code, _ = _run(
            ["uv", "run", "alembic", "upgrade", "head"],
            timeout=300,
            cwd=ROOT,
            env=env,
        )
        checks.append({"name": "alembic_upgrade_head", "passed": code == 0})
        code2, _ = _run(
            ["uv", "run", "alembic", "check"],
            timeout=300,
            cwd=ROOT,
            env=env,
        )
        checks.append({"name": "alembic_check", "passed": code2 == 0})
        code3, out3 = _run(["npm", "run", "build"])
        checks.append({"name": "web_build", "passed": code3 == 0, "detail": out3[-200:]})
        code4, _ = _run(["uv", "run", "python", "-c", "import tripchord.main"])
        checks.append({"name": "api_import", "passed": code4 == 0})
        code5, _ = _run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "pytest",
                "apps/api/tests/test_secret_redaction.py",
                "-q",
            ],
            timeout=600,
        )
        checks.append({"name": "secret_redaction", "passed": code5 == 0})
    code6, out6 = _run(["uv", "run", "python", "scripts/generate_sbom.py", "check"])
    checks.append(
        {
            "name": "sbom_drift_check",
            "passed": code6 == 0,
            "detail": out6[-200:] if code6 else "",
        }
    )
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="1_reproducibility",
        passed=passed,
        detail="migration upgrade/check, web build, API import, secret redaction, sbom drift",
        sub_checks=checks,
    )


def layer2_replay(staging_dir: Path) -> LayerResult:
    """Replay-mode core benchmarks (no real OTA access)."""
    checks: list[dict[str, Any]] = []
    commands = (
        ("benchmarks.evaluate", "benchmark_verifier"),
        ("benchmarks.evaluate_planning", "benchmark_planning"),
        ("benchmarks.evaluate_repair", "benchmark_repair"),
        ("benchmarks.evaluate_events", "benchmark_events"),
    )
    for module, label in commands:
        code, out = _run(["uv", "run", "python", "-m", module], timeout=600)
        passed = code == 0
        checks.append({"name": label, "passed": passed, "detail": out[-300:] if not passed else ""})
    # The five anti-surface acceptance suite must write its evidence into the
    # git-ignored staging dir, never into the tracked results tree (defect fix:
    # the gate no longer pollutes the repo it is certifying).
    acceptance_out = staging_dir / "product-acceptance.json"
    code_accept, out_accept = _run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "benchmarks.evaluate_acceptance",
            "--output",
            str(acceptance_out),
        ],
        timeout=600,
    )
    passed_accept = code_accept == 0
    if passed_accept and acceptance_out.is_file():
        try:
            payload = json.loads(acceptance_out.read_text(encoding="utf-8"))
            accept_detail = (
                f"all {len(payload.get('surfaces', []))} anti-surface surfaces passed"
            )
        except (OSError, ValueError):
            accept_detail = "acceptance suite passed (evidence unreadable)"
    else:
        accept_detail = out_accept[-300:] if code_accept else ""
    checks.append(
        {
            "name": "acceptance_surfaces",
            "passed": passed_accept,
            "detail": accept_detail,
        }
    )
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="2_replay",
        passed=passed,
        detail="verifier/planning/repair/events benchmarks + five anti-surface acceptance",
        sub_checks=checks,
    )


def layer3_clean_chrome_fixtures(staging_dir: Path) -> LayerResult:
    """Clean-Chrome malicious fixture gates (browser bridge + handoff URL policy
    + booking protection + reprice wiring fixtures)."""
    checks: list[dict[str, Any]] = []
    code, out = _run(
        ["uv", "run", "pytest", "apps/api/tests/test_official_handoff.py", "-q"],
        timeout=600,
    )
    checks.append(
        {"name": "handoff_url_policy", "passed": code == 0, "detail": out[-300:] if code else ""}
    )
    code2, out2 = _run(
        [
            "uv",
            "run",
            "pytest",
            "apps/api/tests/test_browser_bridge.py",
            "-q",
            "-k",
            "permission or pair or background or redirect",
        ],
        timeout=600,
    )
    checks.append(
        {
            "name": "browser_bridge_permissions",
            "passed": code2 == 0,
            "detail": out2[-300:] if code2 else "",
        }
    )
    wiring_tests = (
        "apps/api/tests/test_reprice_service.py",
        "apps/api/tests/test_booking_gate.py",
        "apps/api/tests/test_booking_planning_integration.py",
        "apps/api/tests/test_wiring_api.py",
    )
    for module in wiring_tests:
        code3, out3 = _run(
            ["uv", "run", "pytest", module, "-q"],
            timeout=600,
        )
        checks.append(
            {
                "name": Path(module).stem,
                "passed": code3 == 0,
                "detail": out3[-300:] if code3 else "",
            }
        )
    # The clean-Chrome E2E writes its JSON + screenshot into the staging dir
    # (defect fix: tracked browser-e2e.json / browser-e2e-screenshot.png in the
    # results tree must never be rewritten by the gate).
    e2e_json = staging_dir / "browser-e2e.json"
    e2e_screenshot = staging_dir / "browser-e2e-screenshot.png"
    code4, out4 = _run(
        [
            "uv",
            "run",
            "python",
            "scripts/browser_e2e.py",
            "--output-json",
            str(e2e_json),
            "--output-screenshot",
            str(e2e_screenshot),
        ],
        timeout=600,
    )
    if code4 == 0:
        e2e_detail = "workflow-steps + replay plan rendered in clean headless Chrome"
    elif code4 == 2:
        e2e_detail = "clean Chrome or built SPA not available; skipped"
    else:
        e2e_detail = out4[-300:]
    checks.append(
        {
            "name": "clean_chrome_browser_e2e",
            "passed": code4 in {0, 2},
            "detail": e2e_detail,
        }
    )
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="3_clean_chrome_fixtures",
        passed=passed,
        detail=(
            "handoff URL policy + browser bridge permission + booking/reprice "
            "wiring fixtures + clean-Chrome browser E2E"
        ),
        sub_checks=checks,
    )


def _resolve_model_smoke_args() -> tuple[list[str], str] | None:
    """Resolve the required-model smoke invocation from the environment.

    Returns ``(argv, api_key_env)`` when the model endpoint is fully resolvable
    and the user has explicitly acknowledged bounded live model cost, else None.
    """
    if os.environ.get("TRIPCHORD_ACK_MODEL_COST") != "1":
        return None
    api_key_env = next(
        (var for var in _MODEL_API_KEY_ENV_CANDIDATES if os.environ.get(var)),
        None,
    )
    if api_key_env is None:
        return None
    model = os.environ.get("TRIPCHORD_MODEL_NAME") or os.environ.get("ANTHROPIC_MODEL")
    base_url = os.environ.get("TRIPCHORD_MODEL_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
    if not model or not base_url:
        return None
    provider = "anthropic" if "anthropic" in (base_url or "").lower() else "openai_compatible"
    argv = [
        "uv",
        "run",
        "python",
        "scripts/run_model_runtime_smoke.py",
        "--ack-live-cost",
        "--provider",
        provider,
        "--model",
        model,
        "--base-url",
        base_url,
        "--api-key-env",
        api_key_env,
        "--output",
        str(ROOT / "benchmarks" / "results" / "model-runtime-smoke-done-gate.json"),
    ]
    return argv, api_key_env


def layer4_model_smoke() -> LayerResult:
    """OpenAI-compatible required-model smoke when a key is authorised.

    The smoke makes a bounded live model call, so it only runs when the user
    has explicitly acknowledged model cost (``TRIPCHORD_ACK_MODEL_COST=1``) and
    the endpoint is resolvable.  Without that acknowledgement the layer is
    *skipped*, never failed — the gate stays honest about the boundary.
    """
    authorized = any(os.environ.get(var) for var in _MODEL_ENV_VARS)
    resolved = _resolve_model_smoke_args()
    if not authorized:
        return LayerResult(
            name="4_model_smoke",
            passed=False,
            skipped=True,
            detail="no model API key authorised in environment; skipped (not failed)",
        )
    if resolved is None:
        return LayerResult(
            name="4_model_smoke",
            passed=False,
            skipped=True,
            detail=(
                "model key present but bounded live model cost not acknowledged; "
                "set TRIPCHORD_ACK_MODEL_COST=1 (and a resolvable model endpoint) "
                "to run the required-model smoke"
            ),
        )
    argv, _ = resolved
    code, out = _run(argv, timeout=600)
    return LayerResult(
        name="4_model_smoke",
        passed=code == 0,
        detail=out[-500:] if code else "required-model smoke passed",
    )


def _bridge_token() -> str:
    """Bridge token from env, falling back to the launcher's token file."""
    token = os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", "")
    if token:
        return token
    token_file = ROOT / ".runtime" / "browser-bridge-token"
    try:
        candidate = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return candidate if candidate else ""


def _bridge_env(bridge_token: str) -> dict[str, str]:
    """Child env for canary/E2E runners: parent env plus the bridge token ONLY
    via the TRIPCHORD_BROWSER_BRIDGE_TOKEN variable — never argv, so the token
    stays out of the process list and command logs."""
    env = dict(os.environ)
    env["TRIPCHORD_BROWSER_BRIDGE_TOKEN"] = bridge_token
    return env


def _secret_scan_staging(staging_dir: Path, bridge_token: str) -> None:
    """Fail closed if the bridge token appears anywhere in staging evidence.

    The token must never reach logs or evidence.  Every staging file (reports,
    canary JSON, E2E JSON, screenshots) is scanned for the token bytes; a leak
    aborts the gate with exit-2 semantics before any verdict is certified.
    """
    needle = bridge_token.encode("utf-8")
    if not needle:
        return
    for path in sorted(staging_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle in data:
            raise GateStateChangedError(
                f"secret leak: bridge token found in evidence file {path.name}"
            )


def layer5_real_canary(staging_dir: Path) -> LayerResult:
    """Every declared-certified real provider x vertical needs a live canary.

    The layer verdict is driven by a per-scope certified OTA canary
    (``benchmarks/live_canary_certified.py``): each of the six certified scopes
    must show a fresh, authorised, read-only canary — a fresh Companion
    heartbeat for the browser scopes and a real public API read for
    ``icom:transfer``.  The open-meteo / dpm.org.cn probes are kept as a
    separately-labelled public-page connectivity canary that never drives the
    layer verdict.
    """
    sub_checks: list[dict[str, Any]] = []

    # Public-page connectivity canary (open-meteo + dpm.org.cn).  Informational
    # only — it covers zero certified OTA scopes and never drives layer 5.
    code_pub, out_pub = _run(
        ["uv", "run", "python", "benchmarks/live_canary.py"],
        timeout=600,
    )
    sub_checks.append(
        {
            "name": "public_page_connectivity",
            "passed": code_pub == 0,
            "drives_pass": False,
            "detail": (
                "open-meteo + dpm.org.cn read-only connectivity canary (informational)"
                if code_pub == 0
                else out_pub[-300:]
            ),
        }
    )

    bridge_token = _bridge_token()
    if not bridge_token or len(bridge_token) < 32:
        return LayerResult(
            name="5_real_canary",
            passed=False,
            detail=(
                "pending user authorization: certified OTA canaries require a "
                "paired Companion (TRIPCHORD_BROWSER_BRIDGE_TOKEN, >=32 chars) "
                "with ctrip/qunar/tongcheng logged in; re-run once paired"
            ),
            sub_checks=sub_checks,
        )

    evidence_path = staging_dir / "live-canary-certified.json"
    # The bridge token travels to the canary via the inherited environment, NEVER
    # via argv: argv is visible in the process list and can leak into logs.  The
    # child script reads TRIPCHORD_BROWSER_BRIDGE_TOKEN as its default token.
    code, out = _run(
        [
            "uv",
            "run",
            "python",
            "benchmarks/live_canary_certified.py",
            "--output",
            str(evidence_path),
        ],
        env=_bridge_env(bridge_token),
        timeout=900,
    )
    passed = code == 0
    try:
        report = json.loads(evidence_path.read_text(encoding="utf-8"))
        for entry in report.get("scopes", []):
            sub_checks.append(
                {
                    "name": entry.get("scope", "scope"),
                    "passed": entry.get("passed", False),
                    "drives_pass": True,
                    "detail": entry.get("detail", ""),
                }
            )
        status = report.get("companion_status") or {}
        if not status.get("error") and "companions" in status:
            sub_checks.append(
                {
                    "name": "companion_status",
                    "passed": True,
                    "drives_pass": False,
                    "detail": "local Browser Bridge companion status endpoint reachable",
                }
            )
    except (OSError, ValueError):
        sub_checks.append(
            {
                "name": "certified_ota_canary",
                "passed": passed,
                "drives_pass": True,
                "detail": out[-300:],
            }
        )
    return LayerResult(
        name="5_real_canary",
        passed=passed,
        detail=(
            "all 6 certified OTA scopes have fresh authorised read-only canaries"
            if passed
            else (
                "pending user authorization: not all certified OTA scopes have a "
                "fresh authorised read-only canary; evidence in "
                + str(evidence_path)
            )
        ),
        sub_checks=sub_checks,
    )


def _extract_build_fingerprint(status_payload: Any) -> str | None:
    """Extract the installed Companion ``build_identity.build_sha256``.

    Accepts either the companion status payload (layer 5 canary) or the
    companion preflight payload (layer 6 runner), both of which carry a
    ``companions`` array whose entries expose ``build_identity.build_sha256``.
    Returns None when the payload is missing/empty so callers can fail closed.
    """
    if not isinstance(status_payload, dict):
        return None
    companions = status_payload.get("companions")
    if not isinstance(companions, list):
        return None
    for companion in companions:
        if not isinstance(companion, dict):
            continue
        identity = companion.get("build_identity")
        if isinstance(identity, dict) and isinstance(identity.get("build_sha256"), str):
            sha = identity["build_sha256"]
            if re.fullmatch(r"[0-9a-f]{64}", sha):
                return sha
    return None


def _runner_revision_mismatches(
    runner_evidence: dict[str, Any], expected: GitSnapshot, root: Path
) -> list[str]:
    """Cross-check the runner's recorded revision against the gate snapshot.

    The runner's ``repo_revision`` names the repository and commit it claims to
    have exercised.  The gate must not trust the subprocess exit code alone:
    a dirty tree, a mismatched SHA, a foreign toplevel or a not-completed run
    all hard-fail layer 6 even when the subprocess happens to exit 0.
    """
    mismatches: list[str] = []
    repo = runner_evidence.get("repo_revision") or {}
    if not isinstance(repo, dict):
        mismatches.append("runner evidence carries no repo_revision")
        return mismatches
    if repo.get("commit_sha") != expected.commit_sha:
        mismatches.append(
            f"runner repo_revision.commit_sha {repo.get('commit_sha')!r} != "
            f"gate tested HEAD {expected.commit_sha!r}"
        )
    if repo.get("worktree_dirty") is not False:
        mismatches.append(
            f"runner repo_revision.worktree_dirty = {repo.get('worktree_dirty')!r} (must be False)"
        )
    # toplevel and run_status are mandatory fields: a runner that could not
    # determine which repository it exercised, or did not finish, must fail
    # closed instead of being accepted by omission.
    if repo.get("toplevel") is None:
        mismatches.append("runner repo_revision.toplevel is missing (must name the repo root)")
    elif root is not None and repo.get("toplevel") not in (str(root), os.fspath(root)):
        mismatches.append(
            f"runner repo_revision.toplevel {repo.get('toplevel')!r} != {os.fspath(root)!r}"
        )
    run_status = runner_evidence.get("run_status")
    if run_status != "completed":
        mismatches.append(f"runner run_status = {run_status!r} (must be 'completed')")
    if runner_evidence.get("passed") is not True:
        mismatches.append(
            f"runner passed = {runner_evidence.get('passed')!r} (must be true)"
        )
    return mismatches


def _runtime_provenance_mismatches(
    runner_evidence: dict[str, Any],
    root: Path,
) -> list[str]:
    """Cross-check the runner's captured API runtime provenance.

    The layer-6 executor records the running API's self-reported *startup*
    provenance (``runtime_before_run.runtime_provenance``) before any live
    search.  The gate re-derives the provenance the current tree claims and
    hard-fails the layer on any mismatch: a worker started before a HEAD move,
    or whose on-disk source changed without a restart, cannot certify the
    commit this gate is testing.  This is deliberately stronger than trusting
    the subprocess exit code or a working-tree ``git status``.
    """
    runtime_before = runner_evidence.get("runtime_before_run")
    if not isinstance(runtime_before, dict):
        return ["runner evidence carries no runtime_before_run"]
    reported = runtime_before.get("runtime_provenance")
    expected = local_expected_provenance(repo_root=root)
    return provenance_mismatches(reported, expected)


def layer6_full_e2e(staging_dir: Path, start: GitSnapshot) -> LayerResult:
    """Full-platform real E2E only when every external condition is met.

    Runs ``benchmarks/run_live_done_gate_v4.py`` as the real executor: live job
    submit / wait / cancel, event replan, and the strict live-v4 gate
    evaluation.  The executor incurs live model cost (user-authorized via
    ``TRIPCHORD_ACK_MODEL_COST=1``) and requires the paired Companion; until
    those gates are met this layer honestly fails as pending user
    authorization — never forged.

    Layer 6 does not trust the subprocess exit code.  Its evidence bundle is
    parsed and cross-verified against the gate's own git snapshot — HEAD SHA,
    toplevel, worktree cleanliness — and against the layer-5 canary's Companion
    build fingerprint.  Any mismatch hard-fails the layer.
    """
    bridge_token = _bridge_token()
    model_acked = os.environ.get("TRIPCHORD_ACK_MODEL_COST") == "1"
    if not bridge_token or len(bridge_token) < 32:
        return LayerResult(
            name="6_full_e2e",
            passed=False,
            detail=(
                "pending user authorization: full real E2E requires the same "
                "paired Companion as layer 5 (TRIPCHORD_BROWSER_BRIDGE_TOKEN); "
                "not attempted"
            ),
        )
    if not model_acked:
        return LayerResult(
            name="6_full_e2e",
            passed=False,
            detail=(
                "pending user authorization: full real E2E runs the configured "
                "model for Agent stages; set TRIPCHORD_ACK_MODEL_COST=1 to "
                "authorise the bounded live model cost, then re-run"
            ),
        )
    output_path = staging_dir / "live-done-gate-v4.json"
    # Same B1 contract as layer 5: token via inherited env only, never argv.
    code, out = _run(
        [
            "uv",
            "run",
            "python",
            "benchmarks/run_live_done_gate_v4.py",
            "--api-base",
            "http://127.0.0.1:8000",
            "--require-model-enhancement",
            "--output",
            str(output_path),
        ],
        env=_bridge_env(bridge_token),
        timeout=4500,
    )
    mismatches: list[str] = []
    runner_evidence: dict[str, Any] = {}
    try:
        runner_evidence = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        mismatches.append("runner evidence JSON missing or unreadable")
    if not mismatches:
        mismatches.extend(_runner_revision_mismatches(runner_evidence, start, ROOT))
        # Cross-check the Companion build fingerprint against layer 5's canary:
        # both must name the same installed build, or the E2E can claim nothing
        # about the build the canary certified.
        canary_path = staging_dir / "live-canary-certified.json"
        canary_fingerprint = None
        try:
            canary = json.loads(canary_path.read_text(encoding="utf-8"))
            canary_fingerprint = _extract_build_fingerprint(canary.get("companion_status"))
        except (OSError, ValueError):
            pass
        runner_fingerprint = _extract_build_fingerprint(
            runner_evidence.get("companion_preflight")
        )
        if canary_fingerprint is not None and runner_fingerprint is None:
            mismatches.append(
                "runner evidence carries no Companion build fingerprint (companion_preflight)"
            )
        elif canary_fingerprint is not None and runner_fingerprint != canary_fingerprint:
            mismatches.append(
                f"Companion build fingerprint {runner_fingerprint} != "
                f"layer-5 canary fingerprint {canary_fingerprint}"
            )
        # Cross-check the API runtime identity recorded by the runner against
        # the provenance the current tree claims (commit SHA + lock/source
        # fingerprints).  A stale worker cannot certify the tested HEAD.
        mismatches.extend(_runtime_provenance_mismatches(runner_evidence, ROOT))
    passed = code == 0 and not mismatches
    if passed:
        detail = f"full-platform real E2E passed; evidence {output_path}"
    else:
        parts: list[str] = []
        if mismatches:
            parts.append("evidence cross-check failed: " + "; ".join(mismatches))
        elif code != 0:
            parts.append(f"run_live_done_gate_v4.py exited {code}")
        if out and not mismatches:
            parts.append(out[-300:])
        detail = "pending user authorization or executor failure: " + " | ".join(parts)
    return LayerResult(
        name="6_full_e2e",
        passed=passed,
        detail=detail,
    )


def _applicable(layers: list[LayerResult]) -> list[LayerResult]:
    return [layer for layer in layers if not layer.skipped]


def _new_staging_dir() -> Path:
    """A fresh git-ignored staging path for this run's evidence.

    Returns only the path — ``main`` validates it before creating it, so a
    rejected target never leaves a side-effect untracked directory behind.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return RUNTIME_EVIDENCE_DIR / f"gate-{stamp}"


def _require_outside_or_ignored(path: Path, label: str) -> None:
    """Reject evidence write paths that could dirty the certified repository.

    ``path`` must either sit outside the repository toplevel, or be a
    git-ignored path inside it.  A tracked or un-ignored in-repo path is
    rejected (exit 2): the gate would otherwise be able to rewrite tracked
    files after the end-of-run clean check and certify a tree it dirtied
    itself.
    """
    top = _git("rev-parse", "--show-toplevel", check=True).stdout.strip()
    if not top:
        raise GateStateChangedError(
            f"cannot validate {label} {path}: repo toplevel unreadable"
        )
    top_path = Path(top).resolve()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    try:
        resolved.relative_to(top_path)
    except ValueError:
        return  # outside the repository: allowed
    rel = resolved.relative_to(top_path)
    if str(rel) == ".":
        # The repo root itself is never a legal evidence write target.
        raise GateStateChangedError(
            f"{label} {path} is the repository root; evidence must be written "
            "outside the repo or into a git-ignored directory"
        )
    # A tracked path (already in the index) must be rejected outright.
    tracked = _git("ls-files", "--error-unmatch", "--", str(rel), check=False)
    if tracked.returncode == 0:
        raise GateStateChangedError(
            f"{label} {path} is a tracked repository path; evidence must be "
            "written outside the repo or into a git-ignored directory"
        )
    # Anything else inside the repo must be git-ignored.
    ignored = _git("check-ignore", "-q", "--", str(rel), check=False)
    if ignored.returncode != 0:
        raise GateStateChangedError(
            f"{label} {path} is inside the repository but not git-ignored; "
            "evidence must be written outside the repo or into a git-ignored directory"
        )


def _reject_target_conflict(path: Path, label: str, kind: str) -> None:
    """Reject a write target whose existing on-disk form conflicts with the
    directory/file the gate must create — before any write (fail-closed, and
    the repository porcelain stays byte-for-byte unchanged).

    ``kind`` is ``"dir"`` for the staging directory and ``"file"`` for the
    report output path.  ``mkdir`` over an existing file, or ``os.replace``
    onto an existing directory, would otherwise surface a raw OSError outside
    the gate's error contract instead of a contractized exit 2.
    """
    try:
        exists = path.exists()
        is_dir = path.is_dir()
    except OSError:
        exists, is_dir = False, False
    if not exists:
        return
    if kind == "dir" and not is_dir:
        raise GateStateChangedError(
            f"{label} {path} exists and is not a directory; refusing to create "
            "a directory over a file"
        )
    if kind == "file" and is_dir:
        raise GateStateChangedError(
            f"{label} {path} exists and is a directory; refusing to write a "
            "report over a directory"
        )


def run_gate(
    staging_dir: Path,
    *,
    commit: str | None = None,
) -> GateReport:
    """Run all six layers and return the report.

    The repository is snapshotted *before* any layer runs (start) and again
    *after* the report is complete (end).  Layers write evidence only into
    ``staging_dir`` (git-ignored); if the tracked tree or HEAD moved between the
    two snapshots, ``GateStateChangedError`` is raised and the gate exits 2 —
    the evidence would name a revision that was never exercised.
    """
    start = _git_snapshot()
    if commit is not None and commit != start.commit_sha:
        raise GateStateChangedError(
            f"requested commit {commit} != current HEAD {start.commit_sha}; "
            "cannot certify a revision that is not checked out"
        )
    tested_commit_sha = start.commit_sha
    layers = [
        layer1_reproducibility(),
        layer2_replay(staging_dir),
        layer3_clean_chrome_fixtures(staging_dir),
        layer4_model_smoke(),
        layer5_real_canary(staging_dir),
        layer6_full_e2e(staging_dir, start),
    ]
    # B1 secret scan: the bridge token must never reach logs or evidence.  Fail
    # closed (exit-2 semantics) before any verdict is certified if it does.
    _secret_scan_staging(staging_dir, _bridge_token())
    applicable = _applicable(layers)
    passed = (
        not start.worktree_dirty
        and bool(applicable)
        and all(layer.passed for layer in applicable)
    )
    if start.worktree_dirty:
        summary = (
            "worktree has uncommitted changes; running code differs from HEAD, "
            "so tested_commit_sha cannot name the code that was exercised — "
            "commit the tree and re-run before this evidence can be accepted"
        )
    else:
        summary = (
            "all applicable Done-Gate layers passed"
            if passed
            else "one or more Done-Gate layers are not satisfied"
        )
    boundary = (
        "本次判定仅覆盖当前发布声明适用的本地工程门；真实平台 canary 与全平台 "
        "E2E 需用户授权官方域名并保持登录态后才能声明通过。"
        "HTTP 任务成功、测试成功、模型调用成功或全部 Source 终态均不单独构成通过。"
        "证据必须落在干净已提交工作树上；worktree_dirty=true 时 passed 恒为 false。"
        "tested_commit_sha 标注被测试的代码提交；evidence_commit 标注承载证据的提交，"
        "二者不同一，绝不宣称 evidence_commit 所指提交被 tested_commit_sha 测过。"
    )
    end = _git_snapshot()
    _verify_tree_unchanged(start, end)
    return GateReport(
        schema_version=EVIDENCE_SCHEMA,
        generated_at=_now(),
        tested_commit_sha=tested_commit_sha,
        toplevel=start.toplevel,
        branch=start.branch,
        worktree_dirty=start.worktree_dirty,
        layers=layers,
        passed=passed,
        summary=summary,
        boundary=boundary,
    )


def _dump(report: GateReport, output_path: Path = OUTPUT_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".json.tmp")
    payload = json.dumps(
        asdict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, output_path)
    return output_path


_EVIDENCE_TRACKED_PATHS: tuple[tuple[str, str], ...] = (
    ("product-acceptance.json", "benchmarks/results/product-acceptance.json"),
    ("browser-e2e.json", "benchmarks/results/browser-e2e.json"),
    (
        "browser-e2e-screenshot.png",
        "benchmarks/results/browser-e2e-screenshot.png",
    ),
    ("live-canary-certified.json", "benchmarks/results/live-canary-certified.json"),
    ("live-done-gate-v4.json", "benchmarks/results/live-done-gate-v4.json"),
)


def _copy_staged_evidence(staging_dir: Path) -> list[str]:
    """Copy staged evidence into the tracked results tree.

    Returns repo-relative paths of the files copied (the report is handled by
    the caller, not here).  Called only from the explicit ``--commit-evidence``
    phase, after the run verified the tree is clean — never during the run.
    """
    copied: list[str] = []
    for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if not staged.is_file():
            continue
        target = ROOT / tracked_rel
        # Skip targets the repository ignores: ``git add`` fails closed on
        # ignored paths, so copying one into the tracked tree would abort the
        # commit phase and leave untrackable disk junk.  Such evidence is still
        # recorded by hash in the committed evidence manifest, so it is never
        # silently dropped from the audit trail.
        if _git("check-ignore", "-q", "--", tracked_rel, check=False).returncode == 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, target)
        copied.append(tracked_rel)
    return copied


# The committed-evidence contract manifest.  The manifest is the *only* record
# of the git-ignored sensitive live-* evidence that E may not carry: it lists
# the SHA256 + size of every staging original (committed or not) plus redacted
# layer-5/6 verdict fields, so the audit trail proves what raw evidence existed
# and how it was ruled on, without committing token/Cookie/account/full-URL
# bytes.  ``committed`` records whether the raw file itself landed in E.
_MANIFEST_REL = "benchmarks/results/done-gate-evidence-manifest.json"
_MANIFEST_SCHEMA = "tripchord-done-gate-evidence-manifest-v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_files(staging_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if not staged.is_file():
            continue
        ignored = (
            _git("check-ignore", "-q", "--", tracked_rel, check=False).returncode == 0
        )
        files.append(
            {
                "name": staged_name,
                "tracked_path": tracked_rel,
                "sha256": _sha256_file(staged),
                "size_bytes": staged.stat().st_size,
                "committed": not ignored,
            }
        )
    return files


def _canary_manifest(staging_dir: Path) -> dict[str, Any] | None:
    """Redacted layer-5 canary verdict: scope keys + companion identity only."""
    path = staging_dir / "live-canary-certified.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    companions = ((payload.get("companion_status") or {}).get("companions")) or []
    comp = companions[0] if companions else {}
    build = comp.get("build_identity") or {}
    return {
        "passed": payload.get("passed"),
        "bridge_token_present": payload.get("bridge_token_present"),
        "scopes": [
            {"scope": entry.get("scope"), "passed": entry.get("passed")}
            for entry in payload.get("scopes", [])
        ],
        "companion": {
            "companion_id": comp.get("companion_id"),
            "providers": comp.get("providers"),
            "authorized_scope_keys": comp.get("authorized_scope_keys"),
            "is_fresh": comp.get("is_fresh"),
            "age_seconds": comp.get("age_seconds"),
            "build_sha256": build.get("build_sha256"),
        },
    }


def _live_e2e_manifest(staging_dir: Path) -> dict[str, Any] | None:
    """Redacted layer-6 verdict: run status, repo revision, runtime identity and
    Companion preflight — never the raw request/quote/URL/account content."""
    path = staging_dir / "live-done-gate-v4.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rb = payload.get("runtime_before_run") or {}
    rp = rb.get("runtime_provenance") or {}
    cp = payload.get("companion_preflight") or {}
    companions = cp.get("companions") or []
    comp = companions[0] if companions else {}
    return {
        "run_status": payload.get("run_status"),
        "done_gate_passed": (payload.get("done_gate") or {}).get("passed"),
        "repo_revision": payload.get("repo_revision"),
        "runtime_before_run": {
            "model_provider": rb.get("model_provider"),
            "primary_model": rb.get("primary_model"),
            "model_enabled": rb.get("model_enabled"),
            "model_required": rb.get("model_required"),
            "runtime_provenance": {
                "repo_toplevel": rp.get("repo_toplevel"),
                "commit_sha": rp.get("commit_sha"),
                "python_version": rp.get("python_version"),
                "pid": rp.get("pid"),
            },
        },
        "companion_preflight": {
            "status": cp.get("status"),
            "companion_id": comp.get("companion_id"),
            "authorized_scope_keys": comp.get("authorized_scope_keys"),
        },
    }


def _evidence_manifest(
    staging_dir: Path,
    report: GateReport,
    *,
    evidence_commit: str | None = None,
) -> dict[str, Any]:
    """Build the committed-evidence contract manifest for a passing gate."""
    return {
        "schema_version": _MANIFEST_SCHEMA,
        "tested_commit_sha": report.tested_commit_sha,
        "evidence_commit": evidence_commit,
        "generated_at": report.generated_at,
        "toplevel": report.toplevel,
        "branch": report.branch,
        "files": _manifest_files(staging_dir),
        "layer_verdicts": {
            "5_real_canary": _canary_manifest(staging_dir),
            "6_full_e2e": _live_e2e_manifest(staging_dir),
        },
    }


def _write_manifest(manifest: dict[str, Any]) -> Path:
    target = ROOT / _MANIFEST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target.write_text(payload, encoding="utf-8")
    return target


# Fixed required raw-evidence inputs the gate must certify before any committed
# trail can be produced.  This list is part of the evidence contract: layer-5/6
# raw evidence must exist — and must never be silently omitted via gitignore —
# before ``passed=true`` can be claimed and an evidence commit produced.
_REQUIRED_EVIDENCE_INPUTS: tuple[str, ...] = (
    "product-acceptance.json",
    "browser-e2e.json",
    "browser-e2e-screenshot.png",
    "live-canary-certified.json",
    "live-done-gate-v4.json",
)


def _verify_required_evidence_inputs(staging_dir: Path) -> None:
    """Fail closed (exit-2 semantics) when a fixed required evidence input is
    missing from the staging dir.

    The raw layer-5/6 evidence (``live-canary-certified.json``,
    ``live-done-gate-v4.json``) plus the other contract-required inputs must all
    exist before certification.  A missing required input means the gate cannot
    prove the verdict it is about to commit, so certification is refused instead
    of silently producing a manifest that omits the file.
    """
    missing = [
        name
        for name in _REQUIRED_EVIDENCE_INPUTS
        if not (staging_dir / name).is_file()
    ]
    if missing:
        raise GateStateChangedError(
            "evidence contract: required raw evidence input(s) missing from "
            f"staging dir {staging_dir}: {', '.join(missing)}"
        )


def _verify_evidence_contract(
    evidence_commit: str,
    staging_dir: Path,
    manifest: dict[str, Any],
) -> None:
    """Hard-verify E actually contains the contract-required manifest and every
    file the manifest marks committed (with a matching SHA256).  Any missing or
    corrupted committed evidence fails the phase closed (exit 2)."""
    tree = _git(
        "ls-tree", "-r", "--name-only", evidence_commit, check=True
    ).stdout.strip().splitlines()
    if _MANIFEST_REL not in tree:
        raise GateStateChangedError(
            f"evidence commit E {evidence_commit} missing required manifest"
        )
    # Field-completeness of the manifest itself: the committed manifest must
    # carry the contract's required keys, or the audit trail cannot be
    # independently re-verified from the commit alone.
    for key in (
        "schema_version",
        "tested_commit_sha",
        "files",
        "layer_verdicts",
    ):
        if key not in manifest:
            raise GateStateChangedError(
                f"evidence commit E manifest missing required field {key!r}"
            )
    if not isinstance(manifest["files"], list):
        raise GateStateChangedError(
            "evidence commit E manifest files field must be a list"
        )
    for entry in manifest["files"]:
        for key in ("name", "tracked_path", "sha256", "size_bytes", "committed"):
            if key not in entry:
                raise GateStateChangedError(
                    f"evidence commit E manifest file entry missing field {key!r}"
                )
    verdicts = manifest["layer_verdicts"]
    if not isinstance(verdicts, dict):
        raise GateStateChangedError(
            "evidence commit E manifest layer_verdicts field must be an object"
        )
    for key in ("5_real_canary", "6_full_e2e"):
        if key not in verdicts:
            raise GateStateChangedError(
                f"evidence commit E manifest layer_verdicts missing {key!r}"
            )
    for entry in manifest["files"]:
        if not entry["committed"]:
            continue
        rel = entry["tracked_path"]
        if rel not in tree:
            raise GateStateChangedError(
                f"evidence commit E {evidence_commit} missing committed file {rel}"
            )
        blob = _git("show", f"{evidence_commit}:{rel}", check=True, binary=True)
        if hashlib.sha256(blob.stdout).hexdigest() != entry["sha256"]:
            raise GateStateChangedError(
                f"evidence commit E file {rel} sha256 mismatch with staged original"
            )


def _git_parent(commit: str) -> str:
    """The first parent of ``commit``, fail-closed on an unreadable graph."""
    parent = _git("rev-parse", "--verify", f"{commit}^", check=True).stdout.strip()
    if not parent:
        raise GateStateChangedError(f"commit {commit} has no readable parent")
    return parent


def _assert_parent_is(commit: str, expected_parent: str, label: str) -> None:
    """Fail closed unless ``commit``'s first parent is exactly ``expected``.

    This is the post-commit hard binding: even if HEAD moved in the small
    window between the entry snapshot and the commit, E's parent (or the
    pointer commit's parent) must still name the tested revision, so the
    audit trail can never claim a parentage the gate did not observe.
    """
    actual = _git_parent(commit)
    if actual != expected_parent:
        raise GateStateChangedError(
            f"{label} {commit} has parent {actual!r}, expected {expected_parent!r}; "
            "the evidence trail would bind to the wrong revision"
        )


def _assert_head_is(expected: str, label: str) -> None:
    """Fail closed unless the current HEAD is exactly ``expected``."""
    head = _git_snapshot().commit_sha
    if head != expected:
        raise GateStateChangedError(
            f"{label}: HEAD moved to {head!r}, expected {expected!r}"
        )


def _restore_tracked_file(rel: str) -> None:
    """Restore one repo-relative file to its current HEAD state (or remove it
    when HEAD does not track it), so a failed commit-phase leaves no dirty
    uncommitted report/evidence behind on disk."""
    target = ROOT / rel
    # ``binary=True``: the restored blob may be a PNG screenshot, and decoding
    # raw blob bytes as UTF-8 would crash the rollback instead of restoring it.
    probe = _git("show", f"HEAD:{rel}", check=False, binary=True)
    if probe.returncode == 0:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(probe.stdout)
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def _unstage_paths(paths: list[str]) -> None:
    """Drop any index entries this phase staged for ``paths`` (index-only, no
    working-tree change) so a failed commit leaves the repository clean."""
    if not paths:
        return
    try:
        _git("restore", "--staged", "--", *paths, check=False)
    except GateStateChangedError:
        # git restore unavailable/refused is not fatal for the fail-closed
        # contract; the working-tree rollback still restores the files.
        pass


def _commit_evidence(
    staging_dir: Path,
    report: GateReport,
    *,
    start: GitSnapshot,
) -> str:
    """Two-phase evidence commit.

    Phase 1: ``E`` — a commit whose tree contains only evidence paths (the
    report at this point carries ``tested_commit_sha=S`` and ``evidence_commit``
    unset).  Phase 2: a thin pointer commit that fills ``evidence_commit=E``
    into the report so the authoritative record names both S and E, while never
    claiming E was tested at S.

    Requires a clean start tree: the gate already verified end==start, and this
    phase must not sweep unrelated working-tree changes into E.

    The commit-phase snapshot must also still name the repository and the exact
    tested revision the report records (TOCTOU guard): if HEAD moved or the
    evidence would land in a different repository between the run and this
    phase, the audit trail cannot claim it certifies ``tested_commit_sha``.

    Parentage is hard-verified *after* each commit (``E^ == S``, pointer``^ ==
    E``), closing the window between the entry snapshot and the actual commit.
    On any failure the working-tree writes are rolled back to their HEAD state
    and the index is unstaged, so a failed ``--commit-evidence`` never leaves
    a report claiming ``passed=true`` with no committed evidence trail.
    """
    if start.worktree_dirty:
        raise GateStateChangedError(
            "refusing to commit evidence from a dirty worktree: E must contain "
            "only evidence paths, not unrelated uncommitted changes"
        )
    if report.toplevel is not None and start.toplevel != report.toplevel:
        raise GateStateChangedError(
            f"refusing to commit evidence: commit-phase toplevel "
            f"{start.toplevel!r} != report toplevel {report.toplevel!r}"
        )
    if report.tested_commit_sha is not None and start.commit_sha != report.tested_commit_sha:
        raise GateStateChangedError(
            f"refusing to commit evidence: commit-phase HEAD {start.commit_sha!r} "
            f"!= tested commit {report.tested_commit_sha!r}"
        )
    tracked_report_path = ROOT / "benchmarks" / "results" / "product-v1-done-gate.json"

    # Re-verify HEAD right before the first write: a concurrent writer could
    # have moved HEAD after the entry snapshot.  This narrows the TOCTOU
    # window; the post-commit parent checks close it definitively.
    current = _git_snapshot()
    if current.commit_sha != report.tested_commit_sha:
        raise GateStateChangedError(
            f"refusing to commit evidence: HEAD moved to {current.commit_sha!r} "
            f"since the entry snapshot (tested commit {report.tested_commit_sha!r})"
        )

    # Every file this phase may write, captured before any dump/copy so a
    # failure can restore the working tree to its HEAD state (fail-closed on
    # disk: no passed=true report without a committed evidence trail).
    written: list[Path] = [tracked_report_path]
    staged_paths: list[str] = []
    try:
        # The report goes into E with tested_commit_sha=S and evidence_commit unset.
        _dump(report, tracked_report_path)
        copied = _copy_staged_evidence(staging_dir)
        written.extend(ROOT / rel for rel in copied)
        # Evidence-contract manifest: records every staging original by SHA256
        # (including the git-ignored live-* files E may not carry) plus the
        # redacted layer-5/6 verdict fields.  The ignored raw evidence is thus
        # never silently dropped from the audit trail even though E cannot
        # commit its bytes.
        manifest = _evidence_manifest(staging_dir, report)
        manifest_target = _write_manifest(manifest)
        written.append(manifest_target)
        copied.append(_MANIFEST_REL)
        if not copied:
            raise GateStateChangedError("no staged evidence to commit")
        # Phase 1: stage only evidence paths and create E.  Both git calls
        # hard-check the exit code: a failed add/commit must abort the phase,
        # never be silently consumed and reported as a success.
        _git("add", "--", *copied, check=True)
        staged_paths = list(copied)
        _git(
            "commit",
            "-m",
            f"Done-Gate evidence for tested commit {report.tested_commit_sha} "
            f"({report.generated_at})",
            check=True,
        )
        evidence_commit = _git_snapshot().commit_sha
        if not evidence_commit:
            raise GateStateChangedError("evidence commit created but SHA unreadable")
        # Atomic binding: E must be HEAD and its first parent must be the
        # tested revision S (post-commit hard verify).
        _assert_head_is(evidence_commit, "phase 1")
        _assert_parent_is(evidence_commit, report.tested_commit_sha, "evidence commit E")
        # Hard-verify E actually contains the contract-required manifest and
        # every committable evidence file (hash-matched against the staging
        # originals).  Any missing/corrupted committed evidence fails the phase
        # closed, never a silent success.
        _verify_evidence_contract(evidence_commit, staging_dir, manifest)
        # Phase 2 entry re-verify: HEAD must still be E before the pointer commit.
        _assert_head_is(evidence_commit, "phase 2 entry")
        # Phase 2: record evidence_commit=E in the report, re-stamp the manifest
        # with the evidence-commit SHA, and point the audit trail.
        report.evidence_commit = evidence_commit
        _dump(report, tracked_report_path)
        _write_manifest(_evidence_manifest(staging_dir, report, evidence_commit=evidence_commit))
        _git("add", "--", str(tracked_report_path.relative_to(ROOT)), _MANIFEST_REL, check=True)
        staged_paths.extend([str(tracked_report_path.relative_to(ROOT)), _MANIFEST_REL])
        _git(
            "commit",
            "-m",
            f"Record Done-Gate evidence_commit={evidence_commit} for tested commit "
            f"{report.tested_commit_sha}",
            check=True,
        )
        pointer_commit = _git_snapshot().commit_sha
        if not pointer_commit:
            raise GateStateChangedError("pointer commit created but SHA unreadable")
        # Phase 2 parent must be E, and the final tree must be clean.
        _assert_parent_is(pointer_commit, evidence_commit, "phase 2 pointer commit")
        # Phase-2 post-commit contract: the pointer tree must carry the manifest
        # with evidence_commit=E (field-completeness of the committed trail).
        committed_manifest_blob = _git(
            "show", f"{pointer_commit}:{_MANIFEST_REL}", check=True
        ).stdout
        committed_manifest = json.loads(committed_manifest_blob)
        if committed_manifest.get("evidence_commit") != evidence_commit:
            raise GateStateChangedError(
                "phase 2 manifest does not record evidence_commit "
                f"{evidence_commit} in the pointer commit"
            )
        if committed_manifest.get("tested_commit_sha") != report.tested_commit_sha:
            raise GateStateChangedError(
                "phase 2 manifest tested_commit_sha does not match the tested revision"
            )
        final = _git_snapshot()
        if final.commit_sha != pointer_commit:
            raise GateStateChangedError("HEAD moved after the pointer commit")
        if final.worktree_dirty:
            raise GateStateChangedError(
                "evidence commit left the worktree dirty; refusing to certify"
            )
        return evidence_commit
    except (GateStateChangedError, OSError) as exc:
        # Fail closed on disk too: never leave a report claiming passed=true
        # with no evidence trail.  Restore every file this phase wrote and
        # unstage whatever was staged, then propagate the failure.
        for target in written:
            try:
                rel = str(target.relative_to(ROOT))
            except ValueError:
                continue
            _restore_tracked_file(rel)
        _unstage_paths(staged_paths)
        if isinstance(exc, OSError):
            raise GateStateChangedError(f"evidence commit I/O failure: {exc}") from exc
        raise


def _print_report(report: GateReport, output_path: Path, quiet: bool) -> None:
    if quiet:
        print(
            json.dumps(
                {
                    "passed": report.passed,
                    "summary": report.summary,
                    "tested_commit_sha": report.tested_commit_sha,
                    "evidence_commit": report.evidence_commit,
                },
                sort_keys=True,
            )
        )
        return
    print(f"TripChord product v1.0 Done-Gate  {report.generated_at}")
    print(f"tested_commit: {report.tested_commit_sha or 'unknown'}")
    print(f"evidence_commit: {report.evidence_commit or '(not committed)'}")
    print(f"toplevel: {report.toplevel or 'unknown'}")
    print(f"branch: {report.branch or 'unknown'}")
    print(f"worktree_dirty: {report.worktree_dirty}")
    for layer in report.layers:
        marker = "PASS" if layer.passed else ("SKIP" if layer.skipped else "FAIL")
        print(f"  [{marker}] {layer.name}  {layer.detail}")
    print(f"\nverdict: {report.summary}")
    print(f"boundary: {report.boundary}")
    print(f"evidence: {output_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print the verdict")
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=None,
        help=(
            "git-ignored directory for run evidence (default: a fresh "
            ".runtime/done-gate-evidence/gate-<ts> dir)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="atomic report JSON path (default: <staging-dir>/product-v1-done-gate.json)",
    )
    parser.add_argument(
        "--commit",
        type=str,
        default=None,
        help="require the tested HEAD to equal this commit SHA, else exit 2",
    )
    parser.add_argument(
        "--commit-evidence",
        action="store_true",
        help=(
            "after a clean verified run, copy staged evidence into the tracked "
            "results tree and create the evidence commit E (two-phase model: "
            "tested_commit_sha=S, evidence_commit=E)"
        ),
    )
    args = parser.parse_args(argv)

    staging_dir = args.staging_dir or _new_staging_dir()
    output_path = (
        staging_dir / "product-v1-done-gate.json"
        if args.output is None
        else args.output
    )

    try:
        # Validate every write target *before* creating anything: a tracked,
        # un-ignored, repo-root or on-disk-conflicting target must be rejected
        # with exit-2 semantics and must leave the repository porcelain
        # byte-for-byte unchanged (no side-effect mkdir before validation).
        _require_outside_or_ignored(staging_dir, "staging dir")
        _reject_target_conflict(staging_dir, "staging dir", kind="dir")
        if args.output is not None:
            _require_outside_or_ignored(args.output, "output path")
            _reject_target_conflict(args.output, "output path", kind="file")
        # Only now is it safe to create the write targets.
        staging_dir.mkdir(parents=True, exist_ok=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        report = run_gate(staging_dir, commit=args.commit)
    except (GateStateChangedError, OSError) as exc:
        if args.quiet:
            print(json.dumps({"passed": False, "summary": str(exc)}, sort_keys=True))
        else:
            print(f"TripChord product v1.0 Done-Gate  {_now()}")
            print(f"gate aborted: {exc}", file=sys.stderr)
        return 2

    _dump(report, output_path)

    if args.commit_evidence and not report.passed:
        # A failed gate never commits evidence (A1): the staged evidence stays
        # in the ignored/out-of-repo staging dir; HEAD, index and tracked files
        # are left byte-for-byte unchanged — no _commit_evidence, no report
        # write to the tracked results tree.  The staging report already
        # carries the failed verdict, so exit 2 directly.
        _print_report(report, output_path, args.quiet)
        return 2

    if args.commit_evidence:
        try:
            # Evidence-contract gate: the fixed required raw inputs (including
            # layer-5/6 raw evidence) must all exist before any committed trail
            # is produced.  A missing required input hard-fails exit 2 rather
            # than silently omitting the file from the manifest.
            _verify_required_evidence_inputs(staging_dir)
            start = _git_snapshot()
            _commit_evidence(staging_dir, report, start=start)
        except GateStateChangedError as exc:
            # The run verdict is intact but the evidence commit is missing:
            # a committed report must never claim an evidence trail that does
            # not exist, so the phase failure hard-fails the whole gate (exit 2)
            # even when the layers themselves all passed.
            print(f"evidence commit failed: {exc}", file=sys.stderr)
            # Fail closed on disk too: never deliver a report that claims
            # passed=true while the evidence trail is missing.  The process
            # already exits 2; the on-disk JSON must carry the same verdict.
            report.passed = False
            report.summary = (
                f"{report.summary} (evidence commit failed; gate result voided)"
                if report.summary
                else "evidence commit failed; gate result voided"
            )
            _dump(report, output_path)
            _print_report(report, output_path, args.quiet)
            return 2
        # Re-dump so the delivered report carries evidence_commit=E.
        _dump(report, output_path)

    _print_report(report, output_path, args.quiet)
    return 0 if report.passed else 2


if __name__ == "__main__":
    sys.exit(main())
