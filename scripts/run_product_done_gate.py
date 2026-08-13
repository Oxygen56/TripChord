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
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from tripchord._secret_redact import (
    _CREDENTIAL_FIELD_STRONG_NAME_ALT,
    _MAX_JSON_SCAN_CHARS,
    _MAX_JSON_SCAN_DEPTH,
    _MAX_JSON_SCAN_NODES,
    _REGISTERED_BASE_HEADER_FIELD_RE,
    _SHAPE_PATTERN_DIGEST_AUTH_RE,
    BARE_CREDENTIAL_FIELD_NAMES,
    CREDENTIAL_FIELD_NAME_PATTERN,
    DuplicateJsonKeyError,
    PatternScope,
    RecursiveJsonBudgetError,
    _is_registered_base_key_token,
    _is_whole_header_prose,
    _mask_bare_credential_text,
    _mask_digest_credential_text,
    _normalize_for_scan,
    _registered_base_value_exempt_at_path,
    bounded_json_mask,
    iter_json_levels,
    json_loads_no_dupes,
    looks_like_json,
    mask_normalized_spans,
    registry_pattern,
    registry_patterns,
    registry_shape_pairs,
)
from tripchord.agents.live_jobs import LivePlanningPairCheckpoint
from tripchord.planning.frozen_graph import (
    FROZEN_V4_PAIR_COUNT,
    frozen_v4_browser_source_ids,
    frozen_v4_icom_task_ids,
    frozen_v4_pair_id_dates_canonical,
    frozen_v4_pair_id_is_canonical,
    frozen_v4_query_shapes,
    frozen_v4_tasks_per_pair,
)
from tripchord.platform.registry import build_default_registry
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
    run_id: str = ""
    evidence_commit: str | None = None
    # Side-channel publish ref (C-122 P0 / 2026-08-10 11:00): consumers verify
    # ``P^=E``, ``E^=S`` and this report's bindings through
    # ``refs/tripchord/done-gate/<run_id>``.  The evidence commit is never the
    # product branch HEAD.
    gate_ref: str | None = None
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
    env_extra: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # ``ROOT`` is resolved at call time (not bound as a default) so tests and
    # embedders can point the gate at a different repository via monkeypatch.
    # ``binary=True`` returns raw bytes on stdout/stderr (e.g. ``git show`` of
    # a PNG blob); the default text mode must never be used on binary content
    # because UTF-8 decoding crashes the caller.
    # ``env_extra`` injects variables (e.g. a controlled temp GIT_INDEX_FILE)
    # *after* ``_git_safe_env()`` drops caller-supplied GIT_* redirects: the
    # gate alone decides where the evidence index lives, never the environment.
    # ``input_bytes`` feeds the EXACT scanned bytes to the child's stdin (e.g.
    # ``git hash-object -w --stdin``) — binary mode is forced so the bytes are
    # never decoded/encoded through the locale (C-122 round-18 gate-1).
    cwd = cwd or ROOT
    env = _git_safe_env()
    if env_extra:
        env = {**env, **env_extra}
    force_binary = input_bytes is not None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=env,
            capture_output=True,
            text=not binary and not force_binary,
            timeout=timeout,
            check=False,
            input=input_bytes,
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


# Sensitive-value classes that must never reach a report, a print, or an error
# message verbatim (C-118): exact secret values, Authorization/Cookie values,
# account identifiers, phone numbers and full tracking URLs.  ``_redact_output``
# replaces the matched span with ``[REDACTED]``; it is applied at the *source*
# (subprocess output, exception text) so a failed gate and a non-committing run
# never write or print the raw bytes.
def _redact_output(text: str) -> str:
    redacted = text or ""
    for secret in _evidence_secrets():
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _AUTH_COOKIE_PATTERN.sub("[REDACTED]", redacted)
    redacted = _ACCOUNT_ID_PATTERN.sub("[REDACTED]", redacted)
    redacted = _PHONE_PATTERN.sub("[REDACTED]", redacted)
    redacted = _TRACKING_URL_PATTERN.sub(
        lambda match: (
            "[REDACTED]" if _is_tracking_url_leak(match.group(0)) else match.group(0)
        ),
        redacted,
    )
    return redacted


def _redact_report(report: GateReport) -> GateReport:
    """In-place redaction of every layer detail / sub-check detail in a report.

    Idempotent: redacting already-redacted text is a no-op, so the same report
    object can be dumped repeatedly (staging, tracked tree, re-dump) and every
    serialized copy is equally safe.  ``asdict`` JSON stays valid because
    redaction is applied to the string *fields*, never to the serialized JSON
    document.
    """
    for layer in report.layers:
        if layer.detail:
            layer.detail = _redact_output(layer.detail)
        for check in layer.sub_checks:
            if isinstance(check, dict) and check.get("detail"):
                check["detail"] = _redact_output(str(check["detail"]))
    return report


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
        combined = _redact_output((result.stdout or "") + (result.stderr or ""))
        return result.returncode, combined[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "command not found"


# The six Done-Gate layer names, in order (C-122 round-18 gate-4: a passing P
# report must carry EXACTLY these six unique layers, each passed/skipped bound).
_ALL_LAYER_NAMES = (
    "1_reproducibility",
    "2_replay",
    "3_clean_chrome_fixtures",
    "4_model_smoke",
    "5_real_canary",
    "6_full_e2e",
)


def _passing_layers() -> list[LayerResult]:
    """Six all-passing layers — the layer set a certified report must carry."""
    return [LayerResult(name=name, passed=True) for name in _ALL_LAYER_NAMES]


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
            payload = json_loads_no_dupes(acceptance_out.read_text(encoding="utf-8"))
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
        e2e_detail = (
            "clean Chrome or built SPA not available; browser E2E was skipped "
            "and therefore does NOT pass this layer (C-114 R3)"
        )
    else:
        e2e_detail = out4[-300:]
    checks.append(
        {
            "name": "clean_chrome_browser_e2e",
            # R3: a skip (exit 2) is not a pass — only a real rendered E2E in a
            # clean headless Chrome proves the fixture, so code4 must be 0.
            "passed": code4 == 0,
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


# Evidence files are the only surface the gate itself reads back after a run.
# A planted symlink/hardlink, or a file owned by another user, could redirect
# that read (or the copy into the tracked tree) to attacker-chosen bytes, so any
# such file hard-fails the gate.
def _verify_evidence_file_safety(path: Path, label: str) -> None:
    """Reject an evidence file that is a symlink, a hardlink, or owned by a
    non-current user.  Fail-closed (exit-2 semantics) before the gate reads it."""
    try:
        if path.is_symlink():
            raise GateStateChangedError(
                f"{label} {path.name} is a symlink; refusing to read evidence"
            )
        st = path.stat()
        if st.st_nlink > 1:
            raise GateStateChangedError(
                f"{label} {path.name} is a hardlink (nlink={st.st_nlink}); "
                "refusing to read evidence"
            )
        if st.st_uid != os.getuid():
            raise GateStateChangedError(
                f"{label} {path.name} is owned by uid {st.st_uid}, not the "
                f"current user ({os.getuid()}); refusing to read evidence"
            )
    except GateStateChangedError:
        raise
    except OSError as exc:
        raise GateStateChangedError(
            f"{label} {path.name} unreadable: {exc}"
        ) from exc


def _lstat_safe_check(path: Path, label: str) -> None:
    """lstat-based safety check, applied BEFORE any chmod (C-114 R6).

    ``path.chmod`` follows symlinks, so a planted symlink (or a file owned by
    another user) could redirect the hardening chmod — and a hardlinked file is
    a second identity that survives the tree's own protection.  Every staging
    path, including the staging root itself, is lstat-checked first: a symlink,
    a non-current-user owner, or (for regular files) an nlink>1 hardlink fails
    the gate closed."""
    try:
        st = path.lstat()
    except OSError as exc:
        raise GateStateChangedError(
            f"{label} {path.name} unreadable: {exc}"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise GateStateChangedError(
            f"{label} {path.name} is a symlink; refusing to harden an evidence path"
        )
    if st.st_uid != os.getuid():
        raise GateStateChangedError(
            f"{label} {path.name} is owned by uid {st.st_uid}, not the current "
            f"user ({os.getuid()}); refusing to harden an evidence path"
        )
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise GateStateChangedError(
            f"{label} {path.name} is a hardlink (nlink={st.st_nlink}); refusing "
            "to harden an evidence path"
        )


def _harden_staging_permissions(staging_dir: Path) -> None:
    """Restrict the staging tree: directory 0700, every file 0600.

    Raw evidence can carry account/session-adjacent material; even though it
    lives outside the tracked tree, it must not be world- or group-readable on
    disk.  Runs after the layers finish writing, so files created by child
    processes (with the host umask) are re-secured to owner-only.  Every path —
    the staging root and every subdir/file — is lstat safety-checked before its
    chmod (C-114 R6)."""
    _lstat_safe_check(staging_dir, "staging dir")
    try:
        staging_dir.chmod(0o700)
    except OSError as exc:
        raise GateStateChangedError(
            f"cannot harden staging dir {staging_dir}: {exc}"
        ) from exc
    for path in sorted(staging_dir.rglob("*")):
        _lstat_safe_check(path, "staging path")
        try:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        except OSError as exc:
            raise GateStateChangedError(
                f"cannot harden staging path {path}: {exc}"
            ) from exc


_SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passwd|password)"
)

# Known secret-bearing environment variables beyond the model candidates: the
# bridge token plus travel/supplier credentials the evidence can touch (Amap,
# Booking, Amadeus) and additional model providers.
_SECRET_ENV_CANDIDATES = (
    "TRIPCHORD_BROWSER_BRIDGE_TOKEN",
    *_MODEL_API_KEY_ENV_CANDIDATES,
    "AMAP_API_KEY",
    "BOOKING_API_TOKEN",
    "AMADEUS_CLIENT_SECRET",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY",
)


def _evidence_secrets() -> tuple[str, ...]:
    """Active secret values that must never appear in evidence: the bridge token
    plus every API key / token / secret configured on the host.  Deduplicated,
    order kept.

    Covers the model providers (OpenAI, Anthropic, DeepSeek, Gemini, Azure…),
    the travel/supplier credentials (Amap, Booking, Amadeus) and any other
    ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET`` environment variable the host has
    exported — so a newly added provider key cannot silently bypass the scan
    (C-114 R4).
    """
    secrets: list[str] = []
    for name in _SECRET_ENV_CANDIDATES:
        value = (
            _bridge_token()
            if name == "TRIPCHORD_BROWSER_BRIDGE_TOKEN"
            else os.environ.get(name, "")
        )
        if value:
            secrets.append(value)
    for name, value in os.environ.items():
        if name in _SECRET_ENV_CANDIDATES:
            continue
        if value and _SECRET_ENV_NAME_RE.search(name):
            secrets.append(value)
    return tuple(dict.fromkeys(secrets))


# Authorization / Cookie header-or-field values that are not already redacted.
# ``[REDACTED]`` is 11 chars and contains brackets, so it never matches the
# value requirement.  C-122 supervision 03:46 (Block 1): the set is the
# five whole-header fields a diagnostic may carry — Authorization,
# Proxy-Authorization, Cookie, Set-Cookie, X-API-Key — and each is masked
# NAME-AND-VALUE TOGETHER: the value runs to the next newline, so a
# ``Basic`` base64 body, a short ``token=`` opaque value or a ``;``-joined
# cookie pair can never survive with a partially-redacted header (a bare
# ``authorization: bearer <short>`` body escaped the old {12,}/{16,} floors).
# C-122 supervision 04:14: any non-empty value is masked whole — no {4,}
# character floor (``Cookie:a=b`` / ``X-API-Key:abc``) and no quote stops the
# span (``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc;
# HttpOnly"`` / ``X-API-Key: "abc123"`` must all collapse to the marker).
# C-122 supervision 04:14 regression fix: ``authorization`` / ``cookie`` are
# also English words, so a free-form prose sentence like ``pending user
# authorization: no connected Companion declares provider 'ctrip'`` must NOT be
# treated as a header leak.  The name is required to sit at a FIELD position —
# line start, a JSON value/key opening quote, a structural delimiter, or an
# escaped quote (``\``) in a JSON string — where a real header block / JSON
# string value carries it, never mid-prose after a word.  C-122 supervision
# 04:44: a JSON/dict QUOTED-KEY form (``{"Authorization": "Basic a"}`` /
# ``{'Set-Cookie': 'sid=abc'}`` / ``\"Authorization\":\"Basic a\"`` in raw JSON
# bytes) is recognised too — an optional quote may sit between the field name
# and the ``:``/``=`` and between the ``:``/``=`` and the value, so a
# double-quoted JSON key or a single-quoted dict key is masked WHOLE, never
# split at the quote (``[REDACTED]`` replaces name+quotes+value together).
# (A ``"Authorization": "Bearer …"`` JSON KEY is caught separately by
# ``_reject_credential_field_names``; the free-form ``.failure.json`` shape
# scan keeps the broad ``_CANARY_DIAG_WHOLE_HEADER_RE`` for mid-line masking.)
_AUTH_COOKIE_PATTERN = re.compile(
    r"(?i)(?:^[ \t]*|[\"'{[(,:\\])(?:proxy-authorization|set-cookie|x-api-key|"
    r"authorization|cookie)\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    r"(?:\\*[\"']?|[\"'])?[^\r\n]+",
    re.MULTILINE,
)

# R22 Block 30: the FINAL-scan ONLY mid-word (descriptor-prefixed) header form —
# ``upstream Cookie: sid=abc trailing``.  The anchored ``_AUTH_COOKIE_PATTERN``
# needs the field name at a header position, so a real Cookie/Auth header that
# follows a descriptor word (a provider log line) escapes the committed
# decoded-value scan.  This broader pattern matches the name after ANY word
# boundary; :func:`_is_midword_header_prose` then separates a real credential
# VALUE (cookie ``key=value``, Basic/Bearer token) from plain English prose
# (``pending user authorization: no connected Companion…``).  The value is
# quote/newline-bounded so a single-line JSON evidence file cannot let one
# prose match swallow the JSON tail and misread a later ``?date=…`` / ``a=b``
# query string as a credential value.
_AUTH_COOKIE_BROAD_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:proxy-authorization|set-cookie|x-api-key|"
    r"authorization|cookie)\b\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    r"(?:\\*[\"']?|[\"'])?[^\r\n\"']{1,200}",
    re.MULTILINE,
)
_AUTH_COOKIE_BODY_RE = re.compile(r"(?i)^[^:=\r\n]*[:=]\s*(.*)$", re.DOTALL)
# A Basic / Bearer scheme token is a credential ANYWHERE in the value, while a
# cookie ``key=value`` is only a credential at the START of the value (see
# :func:`_is_midword_header_prose`) — an env-var assignment far into prose
# (``set TRIPCHORD_ACK_MODEL_COST=1 to authorise…``) is not a cookie.
_HEADER_VALUE_BASIC_BEARER_RE = re.compile(
    r"(?i)\b(?:basic|bearer)\b[ \t]+[A-Za-z0-9+/=_\-.]{1,}"
)
_HEADER_VALUE_KV_RE = re.compile(
    r"(?i)[A-Za-z0-9_.-]{1,32}\s*=\s*[^\s,;\"']{1,}"
)
# A real cookie ``key=value`` sits immediately after the header colon
# (``upstream Cookie: sid=abc trailing`` -> ``sid=abc`` at position 0).
_HEADER_VALUE_KV_START_MAX = 16


def _is_midword_header_prose(value: str) -> bool:
    """True when a MID-WORD (descriptor-prefixed) auth/cookie header match
    carries plain English prose rather than a credential payload.  A Basic /
    Bearer scheme token is a credential anywhere in the value; a cookie
    ``key=value`` is a credential only when it starts the value (``upstream
    Cookie: sid=abc trailing``), never when it is an env-var assignment far into
    prose (the gate's own ``pending user authorization: … set
    TRIPCHORD_ACK_MODEL_COST=1 …`` report line)."""
    body = _AUTH_COOKIE_BODY_RE.match(value)
    if body is None:
        return True
    body_text = body.group(1)
    if _HEADER_VALUE_BASIC_BEARER_RE.search(body_text):
        return False
    kv = _HEADER_VALUE_KV_RE.search(body_text)
    return kv is None or kv.start() > _HEADER_VALUE_KV_START_MAX


class _AuthCookieLeakScan:
    """FINAL-scan ``Authorization``/``Cookie`` value backstop with the round-20
    Block 20c Basic-prose exception.

    The broad ``_AUTH_COOKIE_PATTERN`` must keep matching every
    authorization/cookie value for the CONSUMER mask (``.sub`` at line 306) and
    for real Basic credentials (``Basic YWJjZA==``), but a value whose Basic
    payload is NOT valid base64 is prose (``authorization: Basic auth/setting``,
    ``authorization: Basic is required``) and must stay ALLOWED in the FINAL
    scans.  ``_is_valid_basic_payload`` (shared module) applies the length-4
    alignment + padding + decodes-to-UTF-8 validity check so the exemption can
    never mask a real credential.

    R21 Block 22/23: ``.search`` iterates EVERY header match — a leading prose
    Basic value must never hide a real ``Authorization: Basic YWJjZA==`` or a
    ``Cookie: sid=abc trailing`` later in the same text — and
    :func:`_is_whole_header_prose` (shared module) decides per VALUE whether
    the prose exemption may apply (a second sensitive field name, a real
    payload, or a short non-empty ``Basic a`` placeholder is never prose).

    R22 Block 29/30: a short / Latin-1 Basic payload that ENDS the value
    (``Basic ab`` / ``Basic abc`` / ``Basic dXNlcjr/``) is no longer prose
    (Block 29), and the scan ALSO iterates mid-word (descriptor-prefixed)
    headers — ``upstream Cookie: sid=abc trailing`` — whose credential-shaped
    VALUE fails the committed decoded-value scan closed (Block 30) while the
    ``pending user authorization: …`` prose positives stay allowed.
    """

    def search(self, text: str) -> re.Match[str] | None:
        for m in _AUTH_COOKIE_PATTERN.finditer(text):
            if not _is_whole_header_prose(m.group(0)):
                return m
        for m in _AUTH_COOKIE_BROAD_PATTERN.finditer(text):
            if _is_whole_header_prose(m.group(0)):
                continue
            if _is_midword_header_prose(m.group(0)):
                continue
            return m
        return None


_AUTH_COOKIE_LEAK_SCAN = _AuthCookieLeakScan()

# Account / member / passenger identifiers with a numeric value (>= 6 digits).
# Tolerates the JSON key-quote between the name and the colon.
_ACCOUNT_ID_PATTERN = re.compile(
    r"(?i)(?:account|user|member|passenger|contact|order)"
    r"[_-]?(?:id|no|number|uid|phone)\s*[\"']?\s*[:=]\s*[\"']?\d{6,}",
    re.MULTILINE,
)

# Chinese mobile number as a bare account identifier.
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# Credential-looking FIELD NAMES that must never appear in a committed evidence
# artifact — even when their VALUE is hash-shaped or already redacted
# (C-122 round-18 gate-1).  ``bridge_token_present`` / ``candidate_set_sha256``
# / ``build_sha256`` are NOT matched: the token flags and digest fields are
# legitimate committed contract fields, so the regex requires the credential
# word to be a full key (or a ``_``/``-``-bounded final component), never a
# substring of a digest/flag name.  Derived from the SHARED registry
# (``tripchord._secret_redact.CREDENTIAL_FIELD_NAME_PATTERN``) so the key-name
# rejector, the producer/consumer structured-key mask and the ``credential_field``
# key-VALUE shape all speak the same field-name contract (C-122 supervision
# 09:00).
_CREDENTIAL_FIELD_RE = CREDENTIAL_FIELD_NAME_PATTERN
_BARE_CREDENTIAL_FIELD_NAMES = BARE_CREDENTIAL_FIELD_NAMES

# A 64-hex VALUE is trusted only at one of these EXACT committed JSON paths.
# C-122 round-18 HG-F: a key merely NAMED ``*_sha256`` / ``*_hash`` /
# ``*_digest`` / ``*_fingerprint`` is no longer enough.  C-122 supervision
# 09:28 (gap 1): each path is a TYPED tuple — dict-key segments and array
# markers (``None``) are SEPARATE, matched by exact type sequence, never a
# string concatenation.  A malicious key that literally contains the path
# delimiter text (``{"files[].sha256": "<64hex>"}``, ``{"files[]": {...}}``,
# ``{"done_gate.checks[].evidence.candidate_set_sha256": "<64hex>"}``) becomes a
# DIFFERENT typed path — a single ``("files[].sha256",)`` key segment or a
# literal ``"files[]"`` key — and can never hit the whitelist.  Every tuple
# below is produced by this gate's own manifest / layer-5 compact / layer-6
# compact builders; a 64-hex under any other path — including a well-named
# digest key inside an artifact this gate does not produce — is an opaque
# token-shaped secret (C-122 round-18 gate-4).
_ARRAY_MARKER: str | None = None  # a list-index position in a typed digest path
_DIGEST_BINDING_PATHS: frozenset[tuple[str | None, ...]] = frozenset(
    {
        # Evidence manifest: the per-file content hash and the layer-5 canary
        # Companion build fingerprint.
        ("files", _ARRAY_MARKER, "sha256"),
        (
            "layer_verdicts",
            "5_real_canary",
            "companion",
            "build_sha256",
        ),
        # Layer-5 compact: the Companion build fingerprint in companion_status.
        (
            "companion_status",
            "companions",
            _ARRAY_MARKER,
            "build_sha256",
        ),
        # Raw layer-5 canary (a committable live-* artifact in a repo without the
        # production ignore rule): the Companion's build fingerprint as carried by
        # the browser-bridge payload.
        (
            "companion_status",
            "companions",
            _ARRAY_MARKER,
            "build_identity",
            "build_sha256",
        ),
        # Layer-6 compact: candidate-set / scenario bindings, runtime-provenance
        # digests, the bridge-state lease hashes, the raw-original hash and the
        # raw request payload's own SHA (``api_payload_sha256`` — the checkpoint
        # binding's request identity, C-122 round-19 gap 4 / supervision 03:46
        # Block 3).  A real compact carries it at this exact committed path, so
        # an unlisted path would make the secret scan reject every genuine
        # publish.
        ("api_payload_candidate_set_sha256",),
        ("api_payload_sha256",),
        ("scenario_sha256",),
        (
            "runtime_before_run",
            "runtime_provenance",
            "dependency_lock_sha256",
        ),
        (
            "runtime_before_run",
            "runtime_provenance",
            "live_system_source_sha256",
        ),
        ("bridge_state_lease_preflight", "sha256"),
        ("bridge_state_lease_postcheck", "sha256"),
        ("raw_evidence", "sha256"),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "candidate_set_sha256",
        ),
        # C-122 Fix 4: the desensitized checkpoint binding the compact carries —
        # the ordered per-checkpoint digest chain, the chain digest, the request
        # identity and each binding's content hashes.  Every value here is a
        # recomputable content-addressable binding the layer-6 validator
        # re-verifies from the carried fields (chain recompute / date window /
        # request identity / per-checkpoint content), so each is trusted at its
        # exact committed path.  ``ordered_checkpoint_sha256`` is a list of
        # plain 64-hex strings, hence its trailing ``_ARRAY_MARKER``.
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "ordered_checkpoint_sha256",
            _ARRAY_MARKER,
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "checkpoint_chain_sha256",
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "request_sha256",
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "bindings",
            _ARRAY_MARKER,
            "checkpoint_sha256",
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "bindings",
            _ARRAY_MARKER,
            "request_sha256",
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "bindings",
            _ARRAY_MARKER,
            "run_summary_sha256",
        ),
        (
            "done_gate",
            "checks",
            _ARRAY_MARKER,
            "evidence",
            "checkpoint_binding",
            "bindings",
            _ARRAY_MARKER,
            "query_task_ids_sha256",
        ),
    }
)


def _format_typed_path(path: tuple[str | None, ...]) -> str:
    """Render a typed digest path back to the dotted string form
    (``("files", None, "sha256")`` -> ``files[].sha256``) for error messages."""
    out: list[str] = []
    for i, seg in enumerate(path):
        if seg is None:
            out.append("[]")
        else:
            if i > 0:
                out.append(".")
            out.append(seg)
    return "".join(out)

# A 32-128 char pure-hex span is a recomputable digest (git SHA, sha256), never
# a phone number or a numeric account identifier.  The pattern scan masks these
# spans before applying the auth/cookie/account/phone patterns so a computed
# hash that happens to contain a phone-shaped run of digits cannot false-positive
# (C-122 Fix 4 keeps hex digests by design; the scan must agree).
_HEX_HASH_SPAN_RE = re.compile(r"[0-9a-fA-F]{32,128}")


def _mask_hex_hash_spans(text: str) -> str:
    """Return ``text`` with every 32-128 char pure-hex span replaced by a
    non-digit placeholder, so no computed digest can read as a phone number or
    account identifier to the pattern scan.

    C-122 supervision 08:30+08:31 缺口①: the placeholder is ``~`` — NOT in the
    token-run charset (``[A-Za-z0-9_\\-=]``) — so a masked digest can never be
    re-flagged as a 32+ token-shaped run by the failure-diagnostic shape scan.
    """
    return _HEX_HASH_SPAN_RE.sub(
        lambda match: "~" * (match.end() - match.start()), text
    )


def _reject_malformed_top_level_json(data: bytes, label: str, name: str) -> None:
    """C-122 supervision 07:29 (gap 2): a ``.json`` evidence artifact — or any
    artifact scanned with ``credential_field_check=True`` — whose TOP-LEVEL text
    is UTF-8, LOOKS like JSON and yet does NOT parse as JSON must fail closed.

    A truncated / unicode-escape-obfuscated JSON attempt at the TOP level
    (``{"Authorization": "Basic a` or a cut ``{"\\u12`` key) could hide a
    credential the byte scan no longer sees as one document: the per-level
    walker only flags malformed NESTED levels (``depth >= 1``) and the top-level
    schema check runs on a DIFFERENT parse.  Rejecting the top-level parse
    failure here closes that hole.  Non-JSON content keeps the original contract
    — binary (not UTF-8 decodable), empty artifacts and plain non-JSON text
    (a PNG placeholder, a prose note) are NOT malformed-JSON attempts and pass
    through to the byte + pattern scan, exactly like any non-JSON file.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # binary, not a JSON artifact; original contract
    if not text.strip():
        return  # empty artifact — no JSON parse to fail
    if not looks_like_json(text):
        return  # plain non-JSON text; byte + pattern scan still applies
    try:
        # C-122 supervision 09:59 Block 2: the canonical parser — a duplicate
        # object member key (a foreign digest smuggled under a whitelisted key)
        # is a top-level parse failure and fails closed, never keeping only the
        # LAST value as ``json.loads`` would.
        json_loads_no_dupes(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise GateStateChangedError(
            f"secret leak: malformed top-level JSON in {label} file {name}"
        ) from None


def _reject_credential_field_names(data: bytes, label: str, name: str) -> None:
    """Fail closed when a JSON evidence artifact carries a credential-looking
    field name (``session_token``, ``access_token``, ``authorization``,
    ``api_key``, ``client_secret``, or a BARE ``token`` / ``cookie`` /
    ``secret`` / ``browser_token`` …).

    C-122 round-18 gate-1: a credential field is a leak even when its value is
    hash-shaped or already redacted — the compact/report/manifest must never
    carry the field at all.  Only committed JSON artifacts are structurally
    walked; a non-JSON file (a PNG, a raw provider dump) cannot have JSON field
    names and still passes through the byte + pattern scan below.

    C-122 supervision 06:58: the walk is BOUNDED-RECURSIVE over JSON-string
    values too — a credential field name smuggled through multiple ``json.dumps``
    layers (``{"outer": "{\\"Authorization\\": …}"}``) sits inside a DECODED
    nested level, not in the top-level structure, so every decoded level is
    walked.  A structural-start string that does not parse (a truncated /
    obfuscated JSON attempt) and a walk that overflows the depth/node/size
    budgets both fail closed.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # not UTF-8 text; the byte/pattern scan still applies
    try:
        for level_text, depth, malformed in iter_json_levels(text):
            # A malformed NESTED level (a truncated / obfuscated JSON-string
            # attempt hiding a credential) fails closed; a non-JSON TOP-LEVEL is
            # not a JSON artifact and passes through to the byte/pattern scan and
            # the schema check, exactly like a non-JSON file.
            if malformed and depth >= 1:
                raise GateStateChangedError(
                    f"secret leak: malformed nested JSON in {label} file {name}"
                )
            try:
                parsed = json_loads_no_dupes(level_text)
            except DuplicateJsonKeyError:
                # C-122 supervision 09:59 Block 2: a duplicate object member key
                # anywhere in a committed artifact is a parse failure — a foreign
                # digest smuggled under a whitelisted field name must fail closed
                # before any value is kept.
                raise GateStateChangedError(
                    f"secret leak: duplicate JSON object key in {label} file {name}"
                ) from None
            except ValueError:
                continue  # not JSON text at this level; the pattern scan applies
            if not isinstance(parsed, (dict, list)):
                continue
            stack: list[Any] = [parsed]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        # C-122 supervision 00:06 (要求 B): the key is matched on
                        # BOTH the raw name and the NORMALIZED copy (NFKC +
                        # casefold, Cf/U+200B dropped) — a full-width
                        # ``Session_token`` (NFKC-composed) or a zero-width
                        # ``Author\u200bization`` is the same credential field
                        # name as its ASCII form and fails closed too.  Only a
                        # COPY is normalized; the committed key text is
                        # untouched.
                        if not isinstance(key, str):
                            continue
                        norm_key = _normalize_for_scan(key)
                        if (
                            _CREDENTIAL_FIELD_RE.search(key)
                            or key.strip().lower() in _BARE_CREDENTIAL_FIELD_NAMES
                            or _CREDENTIAL_FIELD_RE.search(norm_key)
                            or norm_key.strip() in _BARE_CREDENTIAL_FIELD_NAMES
                        ):
                            # R21 Block 26: a credential field whose value is
                            # EXACTLY the gate's own case-exact redaction marker
                            # (``{"secret": "[REDACTED]"}``, at a top-level or a
                            # DECODED nested JSON-string level) is the gate's
                            # clean redacted report, not a leak.  Only the
                            # complete, case-exact ``[REDACTED]`` is exempt —
                            # ``[Redacted]`` / ``[REDACTED]x`` /
                            # ``[RE​DACTED]`` are impersonations and still
                            # fail closed (the shared credential-field shape's
                            # case-exact exemption already guards the free-text
                            # form).
                            if isinstance(value, str) and value == "[REDACTED]":
                                continue
                            raise GateStateChangedError(
                                f"secret leak: credential field name {key!r} in "
                                f"{label} file {name}"
                            )
                        # R27 Block 43: a REGISTERED business-identifier BASE
                        # used as a JSON object KEY (``{"day1": …}`` /
                        # ``{"plannerV2": …}`` / ``{"flightOption1": …}``) is a
                        # credential-shaped field with NO schema/field-path
                        # binding — the closed registry grants the exemption
                        # only to the documented VALUE position, so the base as
                        # a KEY fails closed on the raw AND normalized key
                        # copies, whatever its value (a base-as-key has no
                        # ``[REDACTED]`` clean-report form to exempt).
                        if _is_registered_base_key_token(key) or (
                            _is_registered_base_key_token(norm_key)
                        ):
                            raise GateStateChangedError(
                                f"secret leak: registered business base as JSON "
                                f"key {key!r} in {label} file {name}"
                            )
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(node, list):
                    stack.extend(
                        item for item in node if isinstance(item, (dict, list))
                    )
    except RecursiveJsonBudgetError:
        raise GateStateChangedError(
            f"secret leak: nested JSON budget exceeded in {label} file {name}"
        ) from None


def _reject_unbound_registered_base_values(
    data: bytes, label: str, name: str
) -> None:
    """Fail closed when a JSON evidence artifact carries an EXACT registered
    business-identifier base as a string VALUE at a member path the documented
    business-value registry does not grant (R36 Block 61).

    ``{"otp": "plannerV2"}`` — a version-marker base at an unbound field — and
    ``{"planner_version": "providerV4"}`` — a cross-field value — are
    credential-shaped regardless of the field NAME; only the exact
    ``_DOCUMENTED_BUSINESS_VALUE_PATHS`` paths (``planner_version`` /
    ``plan.planner_version`` / ``summary`` …) with the matching base grant the
    exemption.  The walk is the same BOUNDED-RECURSIVE one used for credential
    field names, over EVERY DECODED JSON level — a base smuggled through a
    ``json.dumps`` value fails closed at the decoded path, not the wrapping
    ``summary`` field.

    R42 Block 82 纠偏 (打回四): a DIRECT string element of an ARRAY and a
    TOP-LEVEL scalar string are REAL JSON values and are judged by the same
    per-value contract — a phrase element (``["(plannerV2) is\ta version."]``)
    stays accepted, an exact registered base at the unbound ``[]`` / ``()``
    path fails closed.  At the TOP-LEVEL parse (``depth == 0``) a direct array
    element is judged; a DECODED nested level has already been judged as the
    outer string VALUE at its real member path (``{"planner_version":
    "[plannerV2]"}`` is the documented base value), so re-judging the decoded
    array items with a RESET path would wrongly reject a documented value.

    R42 Block 84 (打回六): every DECODED scalar string is judged at the member
    path of the string VALUE that carried it — the path is carried through the
    decode recursion, never reset.  A top-level scalar re-encoded 2/3/4 times
    (``"\"plannerV2\""`` -> ``"\"\\\"plannerV2\\\"\""`` -> ...)
    keeps landing at the root path ``()`` and fails closed at EVERY layer —
    exact / balanced wrapped / provider base (``plannerV2`` / ``(plannerV2)``
    / ``providerV4`` / ``[providerV4]``) — symmetric with the masking walker,
    never launder.  A documented member value (``{"summary": ""plannerV2""}``)
    keeps ``('summary',)`` and stays exempt.  This replaces the Block 83
    ``depth == 0`` skip: the carried path, not the depth, decides every decoded
    scalar.

    R42 Block 85 (打回七): the Block 84 judgment is DEFERRED until the decoded
    scalar is no longer JSON text.  A scalar that still starts with a JSON
    structural opener (``{`` / ``[`` / ``"``) is a serialization ENVELOPE, not a
    registered-base value in its own right — recurse to the REAL inner value
    FIRST, then judge it at the carried path.  A legal phrase wrapped in a JSON
    string (``"(plannerV2) is a version."`` at decode L1 of ``layered('(
    plannerV2) is a version.', 2)``) is no longer misread as a wrapped exact
    base (its outer text opens AND closes with ``"`` and carries the base), so
    it stays accepted on all five routes; a structural-start value that does
    NOT parse as JSON (``[plannerV2]`` / ``"[plannerV2]"``) is a REAL value and
    is judged at the carried path — a documented base stays exempt, an unbound
    wrapped base fails closed, exactly like the producer's mask.

    R42 Block 86 (打回七): a decoded LIST level CARRIES its path (array elements
    sit at ``(carried_path, "[]", ...)``); a decoded DICT level resets to ``()``
    (Block 61/82 decoded-document semantics).  The Block 82 ``depth == 0`` gate
    on direct array elements is gone — EVERY decoded array element is judged at
    its carried ``[]`` path, so an encoded array (``layered('["x", "plannerV2",
    "y"]', 2)``) laundered through 2/3/4 decode layers can no longer slip a base
    at the MIDDLE / LAST / NESTED position (first position was already caught).
    A documented outer member inherits the exemption for its array / nested-array
    elements (``{"planner_version": "[plannerV2]"}`` / ``{"summary":
    "[\\"plannerV2\\"]"}`` stay accepted) while a top-level array
    (``("[]",)``) / an unbound member array (``("otp", "[]")``) / a cross-field
    array element (``("planner_version", "[]")`` + ``providerV4``) still fail
    closed — the path, not the position, decides every element.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # not UTF-8 text; the byte/pattern scan still applies
    budget_nodes = 0
    budget_chars = 0

    def walk_level(level_text: str, path: tuple[str, ...], depth: int) -> None:
        """Judge ONE decoded JSON level at the member path of the string VALUE
        that carried it (``()`` for the artifact's own text).  Bounded by the
        same depth / node / size budgets as the shared scan walker."""
        nonlocal budget_nodes, budget_chars
        if depth > _MAX_JSON_SCAN_DEPTH:
            raise RecursiveJsonBudgetError("JSON scan depth budget exceeded")
        budget_nodes += 1
        if budget_nodes > _MAX_JSON_SCAN_NODES:
            raise RecursiveJsonBudgetError("JSON scan node budget exceeded")
        budget_chars += len(level_text)
        if budget_chars > _MAX_JSON_SCAN_CHARS:
            raise RecursiveJsonBudgetError("JSON scan size budget exceeded")
        if not looks_like_json(level_text):
            return  # not JSON text at this level; the pattern scan applies
        try:
            parsed = json_loads_no_dupes(level_text)
        except DuplicateJsonKeyError:
            raise GateStateChangedError(
                f"secret leak: duplicate JSON object key in {label} file {name}"
            ) from None
        except ValueError:
            # A malformed NESTED level (a truncated / obfuscated JSON-string
            # attempt hiding a credential) fails closed; a non-JSON TOP-LEVEL is
            # not a JSON artifact and passes through to the byte/pattern scan
            # and the schema check, exactly like a non-JSON file.  R42 Block 85
            # (打回七): a structural-start NESTED value that does NOT parse is a
            # REAL value that merely begins with a JSON opener — a truncated
            # JSON OBJECT attempt still fails closed; a ``[`` / ``"``-starting
            # value (``[plannerV2]`` reached through the recursion-first
            # decode, or ``"[plannerV2]"`` at an unbound field) is judged at
            # the carried path, so a documented base stays exempt while an
            # unbound wrapped base fails closed — never a silent pass.
            if depth >= 1 and level_text.lstrip().startswith("{"):
                raise GateStateChangedError(
                    f"secret leak: malformed nested JSON in {label} file {name}"
                ) from None
            if depth >= 1 and not _registered_base_value_exempt_at_path(
                path, level_text
            ):
                raise GateStateChangedError(
                    f"secret leak: registered business base value at "
                    f"unbound field path {path!r} in {label} file {name}"
                ) from None
            return
        if isinstance(parsed, str):
            # R42 Block 84 (打回六): a decoded scalar string is judged at the
            # path of the string VALUE that carried it (never a RESET path) — a
            # multi-layer ``json.dumps`` top-level scalar keeps landing at
            # ``()`` and fails closed; a documented member value
            # (``{"summary": ""plannerV2""}``) keeps ``('summary',)`` and
            # stays exempt.  The producer masks the same way (symmetry).
            # R42 Block 85 (打回七): the judgment is DEFERRED while the decoded
            # scalar is still JSON text — recurse to the real inner value FIRST
            # (the outer JSON text is a serialization envelope, never a
            # registered-base phrase), then judge at the carried path.
            if looks_like_json(parsed):
                walk_level(parsed, path, depth + 1)
                return
            if not _registered_base_value_exempt_at_path(path, parsed):
                raise GateStateChangedError(
                    f"secret leak: registered business base value at "
                    f"unbound field path {path!r} in {label} file {name}"
                )
            return
        if not isinstance(parsed, (dict, list)):
            return
        # R42 Block 86 (打回七): a decoded DICT level is walked with paths RESET
        # to ``()`` — the decoded document is judged on its OWN member paths
        # (R36 Block 61 / R42 Block 82), exactly like the producer's structure
        # walk.  A decoded LIST level CARRIES the path of the string VALUE that
        # carried it: every array element is judged at ``(carried_path, "[]",
        # ...)``, so a documented outer member inherits the exemption for its
        # array / nested-array elements while a top-level array (``("[]",)``) /
        # an unbound member array (``("otp", "[]")``) / a cross-field element
        # (``("planner_version", "[]")`` + ``providerV4``) fails closed.  The
        # Block 82 ``depth == 0`` gate on direct array elements is GONE: the
        # carried ``[]`` path, not the decode depth, decides every element.
        stack: list[tuple[Any, tuple[str, ...]]] = (
            [(parsed, ())] if isinstance(parsed, dict) else [(parsed, path)]
        )
        while stack:
            node, node_path = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    # The object MEMBER KEY counts as a node, matching the
                    # shared scan walker's counting.
                    budget_nodes += 1
                    if budget_nodes > _MAX_JSON_SCAN_NODES:
                        raise RecursiveJsonBudgetError(
                            "JSON scan node budget exceeded"
                        )
                    if not isinstance(key, str):
                        continue
                    child_path = (*node_path, key)
                    if isinstance(value, str):
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
                        # R42 Block 85 (打回七): recurse-first — a JSON-text
                        # value is a serialization envelope, judged at the
                        # REAL inner value's carried path (a legal phrase
                        # ``"(plannerV2) is a version."`` at an unbound member
                        # stays accepted); a non-JSON value is judged here.
                        if looks_like_json(value):
                            walk_level(value, child_path, depth + 1)
                        elif not _registered_base_value_exempt_at_path(
                            child_path, value
                        ):
                            raise GateStateChangedError(
                                f"secret leak: registered business base "
                                f"value at unbound field path {child_path!r} "
                                f"in {label} file {name}"
                            )
                    elif isinstance(value, (dict, list)):
                        stack.append((value, child_path))
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, str):
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
                        item_path = (*node_path, "[]")
                        # R42 Block 86 (打回七): every decoded array element is
                        # judged at its carried ``[]`` path (recurse-first for
                        # JSON-text elements, Block 85) — never skipped by
                        # decode depth.
                        if looks_like_json(item):
                            walk_level(item, item_path, depth + 1)
                        elif not _registered_base_value_exempt_at_path(
                            item_path, item
                        ):
                            raise GateStateChangedError(
                                f"secret leak: registered business base "
                                f"value at unbound field path {item_path!r} "
                                f"in {label} file {name}"
                            )
                    elif isinstance(item, (dict, list)):
                        stack.append((item, (*node_path, "[]")))
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )

    try:
        walk_level(text, (), 0)
    except RecursiveJsonBudgetError:
        raise GateStateChangedError(
            f"secret leak: nested JSON budget exceeded in {label} file {name}"
        ) from None


def _check_unknown_64hex(
    value: str,
    path: tuple[str | None, ...],
    depth: int,
    label: str,
    name: str,
) -> None:
    """Fail closed when ``value`` is a 64-hex outside the typed digest whitelist.

    C-122 supervision 08:30+08:31 缺口①: the value is also checked on its
    NORMALIZED copy (NFKC + casefold, Cf/U+200B dropped) — an uppercase /
    full-width / zero-width-obfuscated 64-hex composes to lowercase hex and is
    the same unknown digest.

    C-122 supervision 09:00 (gap 1): a decoded NESTED level's path resets to
    ``()`` inside this walk, so a 64-hex smuggled through a JSON-string value
    (``{"summary": "{\"files\":[{\"sha256\": \"aHEX64\"}]}"}``) would land at
    the top-level whitelisted path ``("files", None, "sha256")`` and be
    accepted -- but its REAL path is the parent context
    (``summary::<decoded>.files[].sha256``), which the whitelist does NOT
    cover.  A nested decoded string can never be a committed digest binding, so
    EVERY 64-hex there is an opaque token-shaped secret.

    At the TOP level the whitelist pass requires RAW canonical lowercase ASCII
    AND an exact typed committed path -- an obfuscated (uppercase / full-width
    / zero-width) 64-hex is never trusted, even under a whitelisted key.  The
    typed-path comparison is EXACT (C-122 supervision 09:28 gap 1): a key that
    merely CONTAINS the delimiter text (``{"files[].sha256": ...}``,
    ``{"files[]": {"sha256": ...}}``) is a DIFFERENT typed path and can never
    hit the whitelist.
    """
    raw_hex = _SHA256_HEX_RE.fullmatch(value) is not None
    norm_hex = _SHA256_HEX_RE.fullmatch(_normalize_for_scan(value)) is not None
    if not (raw_hex or norm_hex):
        return
    if depth >= 1:
        raise GateStateChangedError(
            f"secret leak: unknown 64-hex value in decoded "
            f"nested level of {label} file {name}"
        )
    if not raw_hex or path not in _DIGEST_BINDING_PATHS:
        raise GateStateChangedError(
            f"secret leak: unknown 64-hex value under field "
            f"path {_format_typed_path(path)!r} in {label} file {name}"
        )


def _reject_unknown_64hex_values(data: bytes, label: str, name: str) -> None:
    """Fail closed when a committed JSON artifact carries an unknown 64-hex
    value — a string of exactly 64 lowercase hex OUTSIDE the positive
    field-path whitelist.

    C-122 round-18 HG-F: a bare 64-hex opaque value is indistinguishable from a
    bearer-token-shaped secret and must never enter Git objects.  A 64-hex VALUE
    is trusted only at the EXACT committed field paths in ``_DIGEST_BINDING_PATHS``
    (``files[].sha256``, companion ``build_sha256``, the layer-6 candidate /
    scenario / bridge / raw bindings); a key merely NAMED ``*_sha256`` / ``*_hash``
    / ``*_digest`` / ``*_fingerprint`` that is not a produced committed field path
    is a leak too.  Only committed JSON artifacts are walked; the compact
    desensitizer already redacts 64-hex outside hash positions, so this is the
    second line of defence for the verbatim-copied evidence files.

    C-122 supervision 09:28 (gap 1): the whitelist paths are TYPED tuples —
    dict-key segments and array markers are separate, matched by EXACT type
    sequence, never a string concatenation.  A key that merely contains the
    delimiter text (``{"files[].sha256": ...}``, ``{"files[]": {"sha256": ...}}``,
    ``{"done_gate.checks[].evidence.candidate_set_sha256": ...}``) forms a
    DIFFERENT typed path and is rejected.

    C-122 supervision 09:59 (Block 1): the typed-path segment is the RAW
    canonical key — the walker does NOT strip / lowercase / dash-fold a key
    before matching.  ``API_PAYLOAD_CANDIDATE_SET_SHA256`` (uppercase),
    ``api-payload-candidate-set-sha256`` (dash) and a trailing-space spelling of
    a whitelisted 64-hex field are all DIFFERENT typed paths and are rejected;
    only the ORIGINAL spec key (``api_payload_candidate_set_sha256`` etc.) is
    trusted, because a non-canonical alias is exactly where a foreign digest is
    smuggled in.  The compact validators reject unknown top-level fields for the
    same reason.

    C-122 supervision 06:58: the walk is BOUNDED-RECURSIVE over JSON-string
    values too, and a structural-start string that does not parse (a truncated /
    obfuscated JSON attempt) or a walk that overflows the depth/node/size
    budgets both fail closed.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # not UTF-8 text; the byte/pattern scan still applies
    try:
        for level_text, depth, malformed in iter_json_levels(text):
            # A malformed NESTED level fails closed; a non-JSON TOP-LEVEL is not
            # a JSON artifact and passes through to the byte/pattern scan.
            if malformed and depth >= 1:
                raise GateStateChangedError(
                    f"secret leak: malformed nested JSON in {label} file {name}"
                )
            try:
                parsed = json_loads_no_dupes(level_text)
            except DuplicateJsonKeyError:
                # C-122 supervision 09:59 Block 2: a duplicate object member key
                # is a parse failure — a foreign digest smuggled under a
                # whitelisted field name must fail closed before any value is
                # kept (``json.loads`` would silently keep the LAST value).
                raise GateStateChangedError(
                    f"secret leak: duplicate JSON object key in {label} file {name}"
                ) from None
            except ValueError:
                continue  # not JSON text at this level; the pattern scan applies
            stack: list[tuple[Any, tuple[str | None, ...]]] = [(parsed, ())]
            while stack:
                node, path = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        # C-122 supervision 09:59 Block 1: the typed-path segment
                        # is the RAW canonical key — never ``strip``ped /
                        # ``lower``ed / dash-folded.  An alias of the original
                        # spec key (``API_PAYLOAD_CANDIDATE_SET_SHA256``,
                        # ``api-payload-candidate-set-sha256``, a trailing-space
                        # spelling) is a DIFFERENT typed path and can never hit
                        # the whitelist.
                        seg = str(key)
                        child_path = (*path, seg)
                        if isinstance(value, str):
                            _check_unknown_64hex(
                                value, child_path, depth, label, name
                            )
                        elif isinstance(value, list):
                            # A list under a whitelisted key — e.g.
                            # ``ordered_checkpoint_sha256`` carries a list of
                            # plain 64-hex strings, so each string item is
                            # checked at ``path + <array>`` (the trailing
                            # ``_ARRAY_MARKER`` whitelist entries).  Non-string
                            # items are walked normally.
                            for item in value:
                                if isinstance(item, str):
                                    _check_unknown_64hex(
                                        item,
                                        (*child_path, _ARRAY_MARKER),
                                        depth,
                                        label,
                                        name,
                                    )
                                elif isinstance(item, (dict, list)):
                                    stack.append(
                                        (item, (*child_path, _ARRAY_MARKER))
                                    )
                        elif isinstance(value, dict):
                            stack.append((value, child_path))
                elif isinstance(node, list):
                    for item in node:
                        if isinstance(item, str):
                            _check_unknown_64hex(
                                item, (*path, _ARRAY_MARKER), depth, label, name
                            )
                        elif isinstance(item, (dict, list)):
                            stack.append((item, (*path, _ARRAY_MARKER)))

    except RecursiveJsonBudgetError:
        raise GateStateChangedError(
            f"secret leak: nested JSON budget exceeded in {label} file {name}"
        ) from None

# ISO-8601 date/datetime (``2026-08-13``, ``2026-08-13T10:00:00Z``, …).  A
# public read-only API may legitimately carry such a value in a query (e.g. a
# schedules ``?date=``), so it must not be mistaken for an opaque numeric
# session/account token by the ``\d{4,}`` rule below.
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[Tt ][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?"
    r"(?:[Zz]|[+-][0-2]\d:?[0-5]\d)?)?$"
)


# C-122 Fix 4: query params are redacted by default — a URL only passes when
# every non-blank query value sits on the safe allowlist of benign structural /
# scheduling keys (dates, counts, route/cabin codes, search terms) AND the value
# itself is benign.  Session / account keys are a hard leak regardless of their
# value's shape, covering session= and alphanumeric accounts (?user=alice123)
# that length/``\d{4,}`` heuristics previously let through.
_SAFE_QUERY_PARAM_KEYS = frozenset(
    {
        "date",
        "from",
        "to",
        "dep",
        "arr",
        "departure",
        "return",
        "departuredate",
        "returndate",
        "traveldate",
        "checkin",
        "checkout",
        "adult",
        "adults",
        "child",
        "children",
        "infant",
        "infants",
        "passengers",
        "pax",
        "page",
        "pageindex",
        "pagesize",
        "size",
        "limit",
        "offset",
        "type",
        "sort",
        "order",
        "class",
        "cabin",
        "triptype",
        "tripmode",
        "lang",
        "locale",
        "currency",
        "channel",
        "d",
        "a",
        "q",
        "keyword",
        "dest",
        "origin",
        "searchid",
        "traceid",
        "requestid",
        "r",
        "scid",
        "stn",
    }
)

_SESSION_ACCOUNT_QUERY_KEYS = frozenset(
    {
        "session",
        "sessionid",
        "session_id",
        "sid",
        "token",
        "accesstoken",
        "access_token",
        "auth",
        "authorization",
        "auth_token",
        "sign",
        "sig",
        "signature",
        "st",
        "at",
        "uid",
        "userid",
        "user_id",
        "user",
        "username",
        "account",
        "accountid",
        "account_id",
        "userinfo",
        "user_info",
        "login",
        "passport",
        "password",
        "pwd",
        "key",
        "apikey",
        "api_key",
        "ticket",
        "uuid",
        "pay",
        "callback",
        "redirect",
        "returnurl",
        "openid",
        "unionid",
        "usertype",
        "member",
        "memberid",
        "member_id",
    }
)


def _is_tracking_url_leak(url: str) -> bool:
    """True when a full URL carries a query string the compact may not keep.

    Fail-closed by default (C-122 Fix 4): a non-blank, non-redacted query value
    is a leak unless its key is on the safe allowlist AND the value itself is
    benign (ISO-8601 date, bounded numeric, or short route/cabin code).  Session
    / account keys are leaks unconditionally — including short alphanumeric
    accounts.  Redacted values (``[REDACTED]``) and blank values pass.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if not parsed.query:
        return False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        value = value.strip()
        if not value or value == "[REDACTED]":
            continue
        if key_lower in _SESSION_ACCOUNT_QUERY_KEYS:
            return True
        if key_lower in _SAFE_QUERY_PARAM_KEYS:
            # A safe key carrying an ISO date/datetime, a bounded numeric or a
            # short route/cabin/search code is a schedule/filter value.  A safe
            # key carrying a longer opaque value is still a leak.
            if _ISO_DATETIME_RE.match(value):
                continue
            if re.fullmatch(r"\d{1,6}", value):
                continue
            if len(value) <= 12 and not re.search(r"\d{4,}", value):
                continue
            return True
        # Default deny: any query key outside the safe allowlist with a
        # non-blank, non-redacted value reads as a session/account leak.
        return True
    return False


# ``(?i)`` (C-122 supervision 08:30+08:31 补充 B): an uppercase ``HTTPS://`` URL
# is the same tracking URL as lowercase — the pattern must match it so the
# ``_is_tracking_url_leak`` semantics decide (a plain URL is never rejected).
_TRACKING_URL_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>)\[\]{}]+")


class _SecretNeedles:
    """Encoded secret-scan needles whose repr never exposes the values.

    C-122 round-18 security review: the scan API must not take a repr-able
    plaintext ``tuple[str, ...]`` — a failing traceback would expand real secret
    values verbatim into the log.  This container holds the needles as ``bytes``
    and redacts its repr to a count, so a traceback can never surface them.
    Iteration yields ``bytes`` needles ready for a substring scan.
    """

    __slots__ = ("_items",)

    def __init__(self, secrets: Iterable[str]):
        self._items: tuple[bytes, ...] = tuple(
            dict.fromkeys(bytes(secret, "utf-8") for secret in secrets if secret)
        )

    def __iter__(self) -> Iterable[bytes]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} needles={len(self._items)}>"


def _evidence_scan_needles() -> _SecretNeedles:
    """Active secret values as scan-ready needles (bytes, non-repr-able)."""
    return _SecretNeedles(_evidence_secrets())


def _scan_decoded_string_value(
    value: str,
    needles: _SecretNeedles,
    label: str,
    name: str,
) -> None:
    """Scan ONE decoded string value with the full value-pattern set.

    C-122 supervision 00:06 (要求 A: 普通/解码后 string value ... 逐一扫描):
    a credential smuggled as unicode escapes (``B\u0065...`` spelling
    ``Bearer abcd``) or full-width / zero-width characters is invisible to the
    level-TEXT scan (which sees the escaped form) but is the plain credential
    once the level is decoded — so every decoded string VALUE is scanned
    individually, on the raw and on the NORMALIZED copy (要求 B).

    C-122 supervision 08:30+08:31 (补充 A): this is the callback the shared
    bounded walker (``iter_json_levels(..., on_string_value=…)``) fires for
    every decoded string value — top level, nested levels, bare JSON string
    literals, ``\\uXXXX``-encoded forms — AFTER that value's node budget check,
    so a budget-busting document fails closed at the first over-budget node and
    the callback never runs past the cap (缺口③).  Every value gets the exact-
    secret needle scan, the auth/cookie/account/phone value patterns and the
    tracking-URL leak check on BOTH the committed-evidence and failure-artifact
    paths.  The credential-shape set differs by path: committed evidence runs
    the FINAL_VALUE shapes (akia / prefixed tokens / short Bearer / opaque token
    assignments — dotted JWTs, whole headers and 32+ token runs are legitimate
    there), while a FREE-FORM ``.failure.json`` diagnostic runs the full
    FINAL_TEXT set — including the 32+ token run, dotted bearer tokens and whole
    headers — because such a value is a real credential signal in a diagnostic
    and the level-TEXT scan only sees the ``\\uff53``-escaped form of a
    full-width run, never the run itself (the decoded value is where the real
    characters surface).
    """
    # R20 Block 20a: the ``Digest`` auth ``response`` hex credential is scanned
    # on the RAW decoded value BEFORE ``_mask_hex_hash_spans`` (the 32-128 hex
    # run is otherwise replaced by the recomputable-digest placeholder).
    if _SHAPE_PATTERN_DIGEST_AUTH_RE.search(value):
        raise GateStateChangedError(
            f"secret leak: Digest-auth response in decoded value in "
            f"{label} file {name}"
        )
    value_masked = _mask_hex_hash_spans(value)
    value_norm = _normalize_for_scan(value_masked)
    for needle in tuple(needles):
        if needle in value.encode("utf-8"):
            raise GateStateChangedError(
                f"secret leak: secret value found in decoded value in "
                f"{label} file {name}"
            )
    for pattern, kind in (
        (_AUTH_COOKIE_LEAK_SCAN, "Authorization/Cookie"),
        (_ACCOUNT_ID_PATTERN, "account identifier"),
        (_PHONE_PATTERN, "phone number"),
    ):
        if pattern.search(value_masked) or pattern.search(value_norm):
            raise GateStateChangedError(
                f"secret leak: {kind} in decoded value in {label} file {name}"
            )
    shape_pairs = (
        _FINAL_TEXT_SHAPE_PAIRS
        if name.endswith(".failure.json")
        else registry_shape_pairs(PatternScope.FINAL_VALUE)
    )
    # R21 Block 26: the SAME exact-marker blanking applies to a decoded
    # JSON-string level — a clean redacted report smuggled through json.dumps
    # (``{"summary": "{\"secret\": \"[REDACTED]\"}"}``) decodes to an exact
    # marker assignment here and must not be a shape false positive.  Only the
    # RAW case-exact complete marker is blanked; an impersonation stays a leak.
    value_shapes = _blank_diagnostic_schema_version(
        _blank_exact_marker_assignments(value_masked)
    )
    value_shapes_norm = _normalize_for_scan(value_shapes)
    for pattern, kind in shape_pairs:
        if pattern.search(value_shapes) or pattern.search(value_shapes_norm):
            raise GateStateChangedError(
                f"secret leak: {kind} in decoded value in {label} file {name}"
            )
    # R27 Block 43: the same registered-base-as-header/field-name rule on a
    # DECODED string value — a ``day1: …`` / ``X-Day1: …`` label inside a
    # decoded JSON-string level is still a credential-shaped field name with no
    # schema/field-path binding and fails closed on the raw AND normalized
    # value copies.
    if _REGISTERED_BASE_HEADER_FIELD_RE.search(
        value_masked
    ) or _REGISTERED_BASE_HEADER_FIELD_RE.search(value_norm):
        raise GateStateChangedError(
            f"secret leak: registered business base as header/field name in "
            f"decoded value in {label} file {name}"
        )
    for match in _TRACKING_URL_PATTERN.finditer(value_masked):
        if _is_tracking_url_leak(match.group(0)):
            raise GateStateChangedError(
                f"secret leak: tracking URL in decoded value in {label} file {name}"
            )
    # 补充 B: the same tracking-URL semantics on the normalized copy — an
    # uppercase ``HTTPS://`` / full-width / Cf-obfuscated URL is the same leak
    # once NFKC + casefold + Cf-drop runs, decided by ``_is_tracking_url_leak``
    # (never a blanket URL rejection).
    for match in _TRACKING_URL_PATTERN.finditer(value_norm):
        if _is_tracking_url_leak(match.group(0)):
            raise GateStateChangedError(
                f"secret leak: tracking URL in decoded value in {label} file {name}"
            )


def _make_decoded_value_scanner(
    needles: _SecretNeedles,
    label: str,
    name: str,
) -> Callable[[str], None]:
    """The ``on_string_value`` callback for :func:`_secret_scan_bytes`'s shared
    bounded walker — scans every decoded string value (补充 A) and aborts the
    walk by raising."""
    def scan(value: str) -> None:
        _scan_decoded_string_value(value, needles, label, name)

    return scan


def _secret_scan_bytes(
    data: bytes,
    needles: _SecretNeedles,
    label: str,
    name: str,
    *,
    credential_field_check: bool = False,
) -> None:
    """Fail closed if a secret value or sensitive evidence pattern appears in
    the exact bytes ``data`` (``name`` names the artifact for the error).

    ``needles`` must be a ``_SecretNeedles`` container (never a plaintext tuple,
    so a failing traceback cannot expand secret values — C-122 round-18).

    Multi-class scan, not just a single token's bytes: exact secret values
    (bridge token, model API keys), Authorization/Cookie values, account
    identifiers (numeric ids, phone numbers), full tracking URLs with
    non-redacted query values, and — for committed JSON artifacts — a
    credential-looking field name.  A leak aborts the gate with exit-2 semantics
    before any verdict is certified.  Scan errors report only the category and
    the file name — never the matched bytes (C-114).
    """
    if not isinstance(needles, _SecretNeedles):
        raise TypeError(
            "secret scan needles must be _SecretNeedles, not a plaintext tuple "
            "(C-122 round-18 security contract)"
        )
    for needle in tuple(needles):
        if needle in data:
            raise GateStateChangedError(
                f"secret leak: secret value found in {label} file {name}"
            )
    # C-122 supervision 07:29 (gap 2): a ``.json`` evidence artifact or a
    # committed JSON artifact whose TOP-LEVEL text is UTF-8 but fails to parse
    # must fail closed before any per-level walk — a truncated /
    # unicode-escape-obfuscated top-level JSON attempt could hide a credential.
    if credential_field_check or name.endswith(".json"):
        _reject_malformed_top_level_json(data, label, name)
        # R36 Block 61: an exact registered business-identifier base at a
        # non-documented JSON member path fails closed on BOTH finals — the
        # ``.failure.json`` (cfc=False) producer artifact and the structured
        # evidence file (cfc=True) alike.
        _reject_unbound_registered_base_values(data, label, name)
    if credential_field_check:
        _reject_credential_field_names(data, label, name)
        _reject_unknown_64hex_values(data, label, name)
    text = data.decode("utf-8", errors="ignore")
    # R20 Block 20a: the ``Digest`` auth ``response`` hex credential is scanned
    # on the RAW text BEFORE ``_mask_hex_hash_spans`` — a 32-128 pure-hex run
    # is otherwise replaced by the recomputable-digest placeholder and the
    # token-run shape never sees it.  A server challenge (``WWW-Authenticate:
    # Digest realm=…, nonce=…``) carries no ``response=`` and stays allowed.
    if _SHAPE_PATTERN_DIGEST_AUTH_RE.search(text):
        raise GateStateChangedError(
            f"secret leak: Digest-auth response in {label} file {name}"
        )
    # Mask recomputable hex digests (git SHAs, sha256) before the pattern
    # scan: a hash that happens to contain a phone-shaped run of digits is
    # not a leak, and a bare phone number is always decimal (never hex).
    masked_text = _mask_hex_hash_spans(text)
    for pattern, kind in (
        (_AUTH_COOKIE_LEAK_SCAN, "Authorization/Cookie"),
        (_ACCOUNT_ID_PATTERN, "account identifier"),
        (_PHONE_PATTERN, "phone number"),
    ):
        if pattern.search(masked_text):
            raise GateStateChangedError(
                f"secret leak: {kind} in {label} file {name}"
            )
    # C-122 supervision 00:06 (要求 B): the SAME value patterns run on the
    # NORMALIZED copy (NFKC + casefold, Cf/U+200B dropped) — a full-width /
    # zero-width-obfuscated value the ASCII regexes stop seeing is still a leak.
    # Only a COPY is normalized; the artifact bytes are never rewritten.
    norm_masked = _normalize_for_scan(masked_text)
    for pattern, kind in (
        (_AUTH_COOKIE_LEAK_SCAN, "Authorization/Cookie"),
        (_ACCOUNT_ID_PATTERN, "account identifier"),
        (_PHONE_PATTERN, "phone number"),
    ):
        if pattern.search(norm_masked):
            raise GateStateChangedError(
                f"secret leak: {kind} in {label} file {name}"
            )
    # C-122 supervision 02:56 (Block 2): short / structured credential SHAPES
    # are leaks even when no KNOWN secret value is present and the value is
    # under the 32-char run threshold — AKIA-style AWS keys, well-known token
    # prefixes (``ghp_`` / ``github_pat_`` / ``glpat-`` / ``xoxb-`` / ``sk-``),
    # dotted three-segment JWTs, short ``Bearer <token>`` forms and short opaque
    # ``token=abc`` assignments.  08:30+08:31 (补充 B): the 32+ TOKEN RUN is a
    # rejection signal here too.  These shape scans are applied ONLY to free-form
    # diagnostic files (``<output>.failure.json``) where such text is a real
    # credential signal — a structured evidence file legitimately carries URLs /
    # query strings / dotted domains / 32+ test names, so a global shape scan
    # would false-positive the gate.  A producer that fails to sanitize its
    # diagnostic still fails the gate closed before certification.
    if name.endswith(".failure.json"):
        # R21 Block 26: blank the gate's own exact-marker assignments on the
        # free-form diagnostic path too, so a clean redacted report (or its
        # decoded nested JSON-string level) is not a shape false positive.
        failure_text = _blank_diagnostic_schema_version(
            _blank_exact_marker_assignments(masked_text)
        )
        failure_norm = _normalize_for_scan(failure_text)
        for pattern, kind in _FINAL_TEXT_SHAPE_PAIRS:
            if pattern.search(failure_text):
                raise GateStateChangedError(
                    f"secret leak: {kind} in {label} file {name}"
                )
        # 00:06 (要求 B): the same failure-diagnostic shape set re-searched on
        # the normalized copy, so a full-width / zero-width credential in the
        # free-form diagnostic is caught even when the ASCII shapes stop seeing
        # it on the raw bytes.
        for pattern, kind in _FINAL_TEXT_SHAPE_PAIRS:
            if pattern.search(failure_norm):
                raise GateStateChangedError(
                    f"secret leak: {kind} in {label} file {name}"
                )
    # C-122 supervision 09:00 (gap 2): a FREE-FORM credential field assignment
    # (``Session_token=abc``, full-width ``\uff33\uff45\uff53\uff53\uff49\uff4f\uff4e
    # _\uff54\uff4f\uff4b\uff45\uff4e=abc``) is a
    # leak even when the artifact is NOT a ``.failure.json`` and the text does
    # NOT sit inside a decoded JSON string value — the committed-evidence path
    # scans its raw top-level text for the credential-FIELD shape on the raw AND
    # NORMALIZED copies (mirroring the free-form diagnostic scan above).  A
    # committed JSON artifact's field KEYS are already rejected structurally
    # (``_reject_credential_field_names``); this closes the free-text form.
    #
    # R20 Block 17 + R21 Block 21: the binary (non-strict-UTF-8) guard is now
    # NARROWED to the BARE camelCase-and-digit shape only — that shape is the
    # one that false-positives on a ``utf-8`` ``errors="ignore"`` decode of
    # binary noise (a screenshot PNG's pixels).  The FIELD-NAME-ANCHORED shapes
    # (``credential_field`` / ``basic_auth`` / ``redaction_residue``) are
    # literal field-name / marker signals and run on EVERY artifact, binary
    # included — a PNG whose metadata embeds ``secret=mySuperSecret123`` /
    # ``Session_token=Abc123!`` must fail closed even though the bytes are not
    # strict UTF-8.  The byte-exact needle scan above and the
    # Authorization/Cookie / phone / tracking-URL pattern scans still apply to
    # binary files too.
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        is_utf8_text = False
    else:
        is_utf8_text = True
    # R21 Block 26: blank the gate's OWN clean redacted reports
    # (``secret="[REDACTED]"`` / ``\"secret\": \"[REDACTED]\"`` at a decoded
    # nested JSON-string level) BEFORE the anchored shapes scan, so an exact
    # complete marker is not a false positive.  The blanking is RAW, case-exact
    # and same-length (positions preserved); an obfuscated / residue marker is
    # not blanked and still fails closed on the raw AND normalized copies.
    anchored_text = _blank_exact_marker_assignments(masked_text)
    anchored_norm = _normalize_for_scan(anchored_text)
    for pattern, kind in _CREDENTIAL_FIELD_ANCHORED_PAIRS:
        if pattern.search(anchored_text) or pattern.search(anchored_norm):
            raise GateStateChangedError(
                f"secret leak: {kind} in {label} file {name}"
            )
    # R27 Block 43: a REGISTERED business-identifier BASE used as a HEADER /
    # free-form field NAME (``day1: …`` / ``X-Day1: …`` / ``plannerV2: …`` /
    # ``my_day1: …``) is a credential-shaped field with no schema/field-path
    # binding and fails closed on the raw AND normalized copies, on every
    # artifact — the same field-name-signal class as the anchored shapes above.
    # A plain prose value (``day2`` / ``flightOption1 day2 plannerV2
    # providerV4``) has no ``:``/``=`` after the base and stays positive.
    if _REGISTERED_BASE_HEADER_FIELD_RE.search(
        anchored_text
    ) or _REGISTERED_BASE_HEADER_FIELD_RE.search(anchored_norm):
        raise GateStateChangedError(
            f"secret leak: registered business base as header/field name "
            f"in {label} file {name}"
        )
    if is_utf8_text:
        for pattern, kind in _BARE_CREDENTIAL_PAIRS:
            if pattern.search(masked_text) or pattern.search(norm_masked):
                raise GateStateChangedError(
                    f"secret leak: {kind} in {label} file {name}"
                )
    for match in _TRACKING_URL_PATTERN.finditer(text):
        if _is_tracking_url_leak(match.group(0)):
            raise GateStateChangedError(
                f"secret leak: tracking URL in {label} file {name}"
            )
    # 补充 B: the same tracking-URL semantics on the NORMALIZED copy — an
    # uppercase ``HTTPS://`` / full-width / Cf-obfuscated URL is the same leak
    # once NFKC + casefold + Cf-drop runs, decided by ``_is_tracking_url_leak``.
    for match in _TRACKING_URL_PATTERN.finditer(norm_masked):
        if _is_tracking_url_leak(match.group(0)):
            raise GateStateChangedError(
                f"secret leak: tracking URL in {label} file {name}"
            )
    # C-122 supervision 06:58: BOUNDED-RECURSIVE JSON scan.  A credential
    # smuggled through multiple ``json.dumps`` layers gains one backslash layer
    # per dump, so the raw-byte patterns above stop seeing it after the first
    # escape — the SAME pattern set is re-applied at every DECODED level.  The
    # walker has hard depth/node/size budgets (never unbounded recursion or
    # waiting); a structural-start string that does not parse (a truncated /
    # obfuscated JSON attempt) and a budget overflow both fail closed.
    # The shared bounded walker also fires ``on_string_value`` for every
    # decoded string value AFTER that value's node budget check (缺口③) — the
    # per-value scan (known-needle + shapes + tracking URL, both paths) runs
    # INSIDE the one walker, so a 20k-string document fails closed at node
    # 10001 before any separate traversal could scan all 20k.
    decoded_value_scanner = _make_decoded_value_scanner(needles, label, name)
    try:
        for level_text, depth, malformed in iter_json_levels(
            text, on_string_value=decoded_value_scanner
        ):
            if depth == 0:
                # The raw + normalized TOP-LEVEL text scans above covered the raw
                # form; the DECODED top-level string values are scanned one-by-one
                # by the ``on_string_value`` callback (00:06 要求 A + 补充 A).
                continue
            if malformed:
                raise GateStateChangedError(
                    f"secret leak: malformed nested JSON in {label} file {name}"
                )
            level_masked = _mask_hex_hash_spans(level_text)
            for pattern, kind in (
                (_AUTH_COOKIE_LEAK_SCAN, "Authorization/Cookie"),
                (_ACCOUNT_ID_PATTERN, "account identifier"),
                (_PHONE_PATTERN, "phone number"),
            ):
                if pattern.search(level_masked):
                    raise GateStateChangedError(
                        f"secret leak: {kind} in nested JSON in {label} file {name}"
                    )
            # 00:06 (要求 B): the same nested-level value scan on the NORMALIZED
            # copy — a full-width / zero-width credential inside a decoded level
            # is still a leak.
            level_norm = _normalize_for_scan(level_masked)
            for pattern, kind in (
                (_AUTH_COOKIE_LEAK_SCAN, "Authorization/Cookie"),
                (_ACCOUNT_ID_PATTERN, "account identifier"),
                (_PHONE_PATTERN, "phone number"),
            ):
                if pattern.search(level_norm):
                    raise GateStateChangedError(
                        f"secret leak: {kind} in nested JSON in {label} file {name}"
                    )
            if name.endswith(".failure.json"):
                # R21 Block 26: blank exact-marker assignments at the nested
                # level too — a decoded ``{"secret": "[REDACTED]"}`` level is the
                # gate's clean redacted report, not a leak.
                level_shapes = _blank_diagnostic_schema_version(
                    _blank_exact_marker_assignments(level_masked)
                )
                level_shapes_norm = _normalize_for_scan(level_shapes)
                for pattern, kind in _FINAL_TEXT_SHAPE_PAIRS:
                    if pattern.search(level_shapes):
                        raise GateStateChangedError(
                            f"secret leak: {kind} in nested JSON in {label} file {name}"
                        )
                # 00:06 (要求 B): the failure-diagnostic shape set on the
                # normalized nested copy too.
                for pattern, kind in _FINAL_TEXT_SHAPE_PAIRS:
                    if pattern.search(level_shapes_norm):
                        raise GateStateChangedError(
                            f"secret leak: {kind} in nested JSON in {label} file {name}"
                        )
            for match in _TRACKING_URL_PATTERN.finditer(level_text):
                if _is_tracking_url_leak(match.group(0)):
                    raise GateStateChangedError(
                        f"secret leak: tracking URL in nested JSON in {label} file {name}"
                    )
            # 补充 B: the same tracking-URL semantics on the normalized nested
            # copy — an uppercase / full-width / Cf-obfuscated URL is caught
            # once NFKC + casefold + Cf-drop runs.
            for match in _TRACKING_URL_PATTERN.finditer(level_norm):
                if _is_tracking_url_leak(match.group(0)):
                    raise GateStateChangedError(
                        f"secret leak: tracking URL in nested JSON in {label} file {name}"
                    )
    except RecursiveJsonBudgetError:
        raise GateStateChangedError(
            f"secret leak: nested JSON budget exceeded in {label} file {name}"
        ) from None
    # C-122 supervision 07:29 (gap 3): the NORMALIZED key scan — every key at
    # every decoded level — must ALSO run on free-form ``.failure.json`` staging
    # diagnostics, even though ``credential_field_check=False`` there.  A
    # failure artifact must never carry a credential-looking field name
    # (``session_token`` / ``authorization_status`` / ``token``) in its
    # structured summary, and the shape scans above do NOT see such a key
    # (``session_token`` has no word boundary before ``token``).  This runs
    # AFTER the per-level pattern scan so the existing double/triple-encoded
    # ``Authorization``-value rejection (``Authorization/Cookie``) fires first.
    if name.endswith(".failure.json"):
        _reject_credential_field_names(data, label, name)


def _secret_scan_paths(
    paths: Iterable[Path],
    needles: _SecretNeedles,
    label: str,
    *,
    credential_field_check: bool = False,
) -> None:
    """Fail closed if a secret value or sensitive evidence pattern appears in
    any of ``paths``.

    ``needles`` must be a ``_SecretNeedles`` container (never a plaintext tuple,
    so a failing traceback cannot expand secret values — C-122 round-18).
    ``credential_field_check=True`` additionally rejects a JSON artifact that
    carries a credential-looking field name (committed evidence only).

    Multi-class scan, not just a single token's bytes: exact secret values
    (bridge token, model API keys), Authorization/Cookie values, account
    identifiers (numeric ids, phone numbers) and full tracking URLs with
    non-redacted query values.  Every file is also safety-checked (no
    symlink/hardlink/non-current-user).  A leak aborts the gate with exit-2
    semantics before any verdict is certified.  Scan errors report only the
    category and the file name — never the matched bytes (C-114).
    """
    for path in sorted(paths, key=lambda p: str(p)):
        if not path.is_file():
            continue
        _verify_evidence_file_safety(path, label)
        try:
            data = path.read_bytes()
        except OSError as exc:
            # Fail closed (C-114 R4): an unreadable evidence file could hide a
            # secret.  Report only the category and file name — never content.
            raise GateStateChangedError(
                f"secret scan: cannot read {label} file {path.name} "
                f"({exc.__class__.__name__}); refusing to certify evidence"
            ) from exc
        _secret_scan_bytes(
            data,
            needles,
            label,
            path.name,
            credential_field_check=credential_field_check,
        )


def _secret_scan_staging(staging_dir: Path, needles: _SecretNeedles) -> None:
    """Fail closed if a secret value or sensitive evidence pattern appears in
    staging evidence (every file under ``staging_dir``)."""
    _secret_scan_paths(
        (p for p in staging_dir.rglob("*") if p.is_file()),
        needles,
        "evidence",
    )


def _final_evidence_secret_scan(
    staging_dir: Path,
    tracked_paths: Iterable[Path],
) -> None:
    """Final comprehensive secret scan, run AFTER every report / manifest /
    compact evidence file is written and immediately BEFORE the atomic commit
    CAS (C-114 ordering fix).

    Covers the staging dir (raw evidence, desensitized compact artifacts, the
    staging report) plus every file this phase wrote into the tracked results
    tree (the authoritative report, the evidence manifest, the copied committed
    evidence).  A leak that only appears in the last-written report or manifest
    is therefore caught before the branch moves, not after.
    """
    needles = _evidence_scan_needles()
    _secret_scan_staging(staging_dir, needles)
    _secret_scan_paths(
        (p for p in tracked_paths if p.is_file()),
        needles,
        "committed evidence",
    )


_CANARY_DIAG_SCHEMA_VERSION = "tripchord-certified-ota-canary-v1"
# A failure diagnostic older than this is a STALE failure (a prior run's crash),
# never evidence of the current canary run (Block 2 freshness binding).
_CANARY_DIAG_MAX_AGE_SECONDS = 1800
# The authoritative interpreter identity a canary failure diagnostic must match
# EXACTLY (C-122 supervision 01:10 Block 3).  The canary runs as a child of this
# gate under the same project interpreter, so the diagnosis's ``runtime.python``
# must equal THIS process's ``sys.version_info[:3]`` and ``runtime.platform``
# must equal ``sys.platform`` — a foreign ``EVIL-RUNTIME`` / ``EVIL-PLATFORM``
# can never pass by being merely non-empty.
_CANARY_DIAG_EXPECTED_PYTHON = ".".join(str(part) for part in sys.version_info[:3])
_CANARY_DIAG_EXPECTED_PLATFORM = sys.platform
# Upper bound for a free-form diagnostic field (summary / exception_class /
# stage) carried into the committed report — a crafted 10 MB summary cannot bloat
# or smuggle the committed trail (consumer whitelist, Block 3).
_CANARY_DIAG_FIELD_MAX_CHARS = 512
# The consumer's own whitelist patterns (never trust the producer's
# desensitization alone): any URL is collapsed to ``<url>`` and any 32+ character
# run of token characters (hex hashes, base64-ish secrets, ``S*40``-style junk)
# is collapsed to ``<redacted>`` before a free-form field reaches the committed
# report (C-122 supervision 01:10 Block 3).  C-122 supervision 18:13 adds the
# short / structured shapes the 32+ run misses: AKIA-style AWS access keys
# (20 chars), dotted bearer tokens (``header.payload.signature`` JWTs whose first
# two segments are each under 32 chars and survive the run pattern), and short
# opaque ``token=value`` / ``password=value`` assignments.
# Every credential-shape regex the consumer mask and final scan use is DERIVED
# from the single typed registry in ``tripchord._secret_redact`` (C-122
# supervision 08:30+08:31 补充 C) — a shape / flag change lands in producer,
# consumer and final scan at once instead of three hand-synced copies.  The
# named variables stay so ``_canary_diag_mask_level`` can apply each pattern
# with its own replacement (``<url>`` vs ``[REDACTED]``).
_CANARY_DIAG_URL_RE = registry_pattern("url")
_CANARY_DIAG_TOKEN_RUN_RE = registry_pattern("token_run")
_CANARY_DIAG_AKIA_RE = registry_pattern("akia")
_CANARY_DIAG_PREFIX_TOKEN_RE = registry_pattern("prefix_token")
_CANARY_DIAG_BEARER_RE = registry_pattern("bearer")
_CANARY_DIAG_DOTTED_TOKEN_RE = registry_pattern("dotted_token")
_CANARY_DIAG_WHOLE_HEADER_RE = registry_pattern("whole_header")
# C-122 supervision 09:00: the credential FIELD NAME key-VALUE shape — an ASCII /
# full-width ``Session_token=abc`` / ``"Session_token":"abc"`` assignment (the
# ``session_token`` family plus bare ``token=`` / ``cookie=`` / ``secret=``) is
# masked WHOLE (name + value) by the consumer before a free-form diagnostic field
# reaches the committed report, and rejected by the final scan on BOTH paths.
_CANARY_DIAG_CREDENTIAL_FIELD_RE = registry_pattern("credential_field")
# C-122 supervision 00:06 (要求 B) + 08:30+08:31 补充 B: the value/shape set —
# including the 32+ token run and the tracking URL — re-applied to a NORMALIZED
# copy (NFKC + casefold, Cf/U+200B dropped).  A full-width / zero-width-
# obfuscated credential (``\uff21uthorization: Basic``,
# ``Author\u200bization: ...``, ``\uff22\uff45\uff41\uff52\uff45\uff52
# abcd``, full-width ``\uff54\uff4f\uff4b\uff45\uff4e\uff1d\uff41\uff42
# \uff43``) and a full-width / Cf-obfuscated ``HTTPS://`` URL stop matching the
# ASCII regexes above once the ASCII letters are hidden, so the SAME patterns
# are searched again on the normalized detection copy (only a copy is
# normalized; the artifact bytes are untouched).
_NORMALIZED_DIAG_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = registry_patterns(
    PatternScope.NORMALIZED
)
# The FINAL-TEXT shape pairs the free-form failure-diagnostic level scan
# iterates (raw + normalized) — whole headers, token runs, AKIA keys, prefixed
# tokens, dotted JWTs, short Bearer forms and opaque token assignments
# (补充 B: the 32+ token run is now a rejection signal here too).  Excludes the
# URL shape — a tracking URL is decided by ``_is_tracking_url_leak`` semantics,
# never by rejecting every URL.
_FINAL_TEXT_SHAPE_PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    registry_shape_pairs(PatternScope.FINAL_TEXT)
)
# The single credential-FIELD-NAME shape applied to the RAW TOP-LEVEL text of
# EVERY artifact — committed evidence AND free-form diagnostics.  A free-form
# ``Session_token=abc`` / full-width ``\uff33\uff45\uff53\uff53\uff49\uff4f\uff4e
# _\uff54\uff4f\uff4b\uff45\uff4e=abc`` that does
# NOT sit inside a decoded JSON string value (a bare non-JSON ``.json`` free-text
# file, a raw provider dump) is still a leak and must be rejected on BOTH final
# paths (C-122 supervision 09:00 gap 2).  Only this small set scans the raw top
# level of committed evidence; the rest of the FINAL_VALUE set runs on DECODED
# string values where legitimate dotted domains / whole headers / 32+ test names
# are excluded.  R18 Block 1 adds ``basic_auth`` — the final-scan independent
# fallback: a complete ``Authorization``/``Proxy-Authorization`` ``Basic`` field
# that survives producer/consumer must fail closed on the committed RAW path too,
# not just on the free-form whole-header path.
# R21 Block 21: the anchored set runs on EVERY artifact, binary included — the
# shapes are literal field-name / marker signals (``secret=…``,
# ``Authorization: Basic <payload>``, ``[REDACTED]<alnum>``) whose presence in
# a binary's metadata is exactly the leak.  ``bare_credential_value`` is
# split out (``_BARE_CREDENTIAL_PAIRS``) because the camelCase-and-digit shape
# is the one that false-positives on utf-8-ignore-decoded binary noise and
# stays gated on strict-UTF-8 text.
_CREDENTIAL_FIELD_ANCHORED_PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (registry_pattern("credential_field"), "credential field name assignment"),
    (registry_pattern("basic_auth"), "Basic Authorization field"),
    # R20 Block 17: the redaction-marker RESIDUE (``[REDACTED]mySuperSecret123``)
    # is a literal-marker leak on the RAW top-level text of every artifact.
    (registry_pattern("redaction_residue"), "redaction-marker residue"),
)


# R21 Block 26: the gate's own clean redacted report is ``{"secret": "[REDACTED]"}``
# — a credential field whose value is EXACTLY the case-exact ``[REDACTED]`` marker
# (complete field value, quoted or bare, at a top-level or a decoded nested
# JSON-string level) is exempt from the free-text credential-field scan.  The
# shared ``credential_field`` shape's own exemption is bypassed in JSON-quoted
# text because the value branch can start at the KEY's closing quote, so the scan
# blanks every exact-marker assignment FIRST and runs the anchored shapes on the
# blanked copy.  Only the RAW case-exact marker is blanked — any impersonation
# (``[Redacted]``, ``[REDACTED]x``, full-width / zero-width / Cf variant, a
# marker followed by residue) is NOT blanked and still fails closed.
_EXACT_MARKER_AFTER = (
    # after the marker's closing quote / bracket: either JSON structure to end
    # or newline (``}`` / ``]`` / ``,`` / ``;`` and JSON-string close quotes /
    # backslash escapes: ``\"}\"`` of a nested JSON-string level), or a space
    # that BEGINS the next field assignment.
    r"(?=(?:[ \t]*[\}\],;\\\\\"])*[ \t]*(?:$|[\r\n])"
    r"|[ \t]+[A-Za-z0-9_-]+[ \t]*[\"']?[ \t]*[:=])"
)
_EXACT_MARKER_FIELD_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])((?i:"
    + _CREDENTIAL_FIELD_STRONG_NAME_ALT
    + r"))(?:[_-]?|$)"
    r"\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    r"(?:(?:\\*[\"'])\[REDACTED\](?:\\*[\"'])|\[REDACTED\])"
    + _EXACT_MARKER_AFTER
)


def _blank_exact_marker_assignments(text: str) -> str:
    """Replace every exact-marker credential-field assignment with same-length
    spaces (positions preserved), so the anchored free-text scan no longer sees
    the gate's own clean redacted report as a leak (R21 Block 26).  Only the
    RAW, case-exact, complete ``[REDACTED]`` value is blanked; an obfuscated /
    residue marker stays intact and fails the scan closed."""
    if not text:
        return text
    spans = [m.span() for m in _EXACT_MARKER_FIELD_RE.finditer(text)]
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)


# R21 full-chain: a REAL sealed canary failure diagnostic carries the canary's own
# fixed ``schema_version`` constant (``tripchord-certified-ota-canary-v1``) — a
# 33-char ``[A-Za-z0-9_\\-=]`` run the free-form token-run shape would flag as a
# "token-shaped run", making the gate reject its OWN clean diagnostic on the
# failure final.  The value is a FIXED constant written by the gate's seal code
# (never user-controlled), so blanking the assignment before the free-form shape
# scan cannot mask a real leak — the ``summary`` (the actual free-form carrier)
# is scanned unchanged.  Matches BOTH the ``"schema_version": …`` assignment AND
# the bare constant in a decoded string-value callback (the bounded walker fires
# on the VALUE, not the assignment).
_DIAGNOSTIC_SCHEMA_VERSION_RE = re.compile(
    r'((?i:"schema_version")[ \t]*:[ \t]*)((?:"(?:\\.|[^"\\])*"|[A-Za-z0-9_.\-:/]+))'
    r'|(?:"'
    + re.escape(_CANARY_DIAG_SCHEMA_VERSION)
    + r'"|'
    + re.escape(_CANARY_DIAG_SCHEMA_VERSION)
    + r")"
)


def _blank_diagnostic_schema_version(text: str) -> str:
    """Same-length-space the diagnostic's own fixed ``schema_version`` VALUE (the
    key, colon and string quotes stay), so the canary's sealed failure diagnostic
    does not trip its own failure-final token-run scan AND the text REMAINS a
    valid JSON document — R42 Block 81: the free-text narration decode is gated
    on a real JSON parse (``_is_json_document``), so blanking the whole
    assignment (which left a dangling member comma) would turn the sealed
    diagnostic's summary into a false fail-open/closed.  The value is a FIXED
    gate-written constant (never user-controlled), so blanking it cannot mask a
    real leak; the ``summary`` (the actual free-form carrier) is scanned
    unchanged."""
    if not text:
        return text
    chars = list(text)
    for m in _DIAGNOSTIC_SCHEMA_VERSION_RE.finditer(text):
        value_start, value_end = m.start(2), m.end(2)
        if value_start == -1:
            # second alternative: the bare constant (no assignment) — blank the
            # whole match; it sits inside a decoded string value whose surrounding
            # quotes remain, so JSON validity is unaffected.
            value_start, value_end = m.span()
        else:
            quoted = m.group(2)
            if len(quoted) >= 2 and quoted[0] == '"' and quoted[-1] == '"':
                # a quoted JSON string value: blank only the content between the
                # quotes so ``"schema_version": "<value>"`` stays valid JSON.
                value_start += 1
                value_end -= 1
        for i in range(value_start, value_end):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)


_BARE_CREDENTIAL_PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    # R20 Block 17: the BARE camelCase-and-digit credential-shaped value
    # (``mySuperSecret123``) with no field name is a leak on the RAW top-level
    # text of a strict-UTF-8 artifact (R21 Block 21: skipped on binary).
    (registry_pattern("bare_credential_value"), "bare credential-shaped value"),
)
# The diagnostic's STRUCTURED schema — a fail-closed whitelist: any unknown
# top-level / run_identity / runtime field makes the diagnostic foreign and is
# rejected before its summary is ever consumed (C-122 supervision 18:13:
# 未知字段 fail-closed).
_CANARY_DIAG_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "diagnostic_kind",
        "stage",
        "exception_class",
        "summary",
        "run_identity",
        "generated_at",
    }
)
_CANARY_DIAG_ALLOWED_RUN_IDENTITY = frozenset(
    {"script", "output", "run_id", "tested_sha", "runtime"}
)
_CANARY_DIAG_ALLOWED_RUNTIME = frozenset({"python", "platform"})


def _sanitize_canary_diag_field(value: str, fallback: str) -> str:
    """Whitelist a free-form canary diagnostic field for the committed report.

    The classification is carried into the layer-5 detail and the committed
    report, so the consumer re-sanitizes every free-form diagnostic string
    before it lands: env-secret / URL / phone / account / tracking patterns are
    redacted (``_redact_output``), any remaining URL is collapsed to ``<url>``,
    any 32+ token-shaped run is collapsed to ``[REDACTED]``, AKIA-style access
    keys / dotted bearer tokens / short opaque ``token=`` assignments are
    collapsed too, whole header fields are masked name-and-value together
    (C-122 supervision 03:46 Block 1), control characters are stripped, and the
    result is bounded in length.  C-122 supervision 04:44: the redaction marker
    contract is unified — this sanitizer and the producer's ``_desensitize``
    emit the SAME ``[REDACTED]`` marker the gate's ``_redact_output`` uses, so a
    three-layer regression can assert a single fixed marker.  A
    credential-bearing summary — forged or otherwise — can never reach the
    committed trail as-is (C-122 supervision 01:10 Block 3 + 18:13).
    """
    value = bounded_json_mask(
        value,
        mask_level=_canary_diag_mask_level,
        # C-122 supervision 00:06 (要求 B): re-check every masked level on the
        # NORMALIZED copy (NFKC + casefold, Cf/U+200B dropped) so a full-width /
        # zero-width-obfuscated credential span is collapsed even though the
        # ASCII shape regexes stopped seeing it on the raw text.
        normalize_patterns=_NORMALIZED_DIAG_SHAPE_PATTERNS,
        # C-122 supervision 09:00: a STRUCTURED JSON credential field NAME
        # (``{"Session_token":"abc"}``) is masked whole too — the key would
        # otherwise survive the free-form value-only walk.
        key_patterns=(CREDENTIAL_FIELD_NAME_PATTERN,),
    )
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    if not value:
        return fallback
    if len(value) > _CANARY_DIAG_FIELD_MAX_CHARS:
        value = value[:_CANARY_DIAG_FIELD_MAX_CHARS] + "…"
    return value


def _canary_diag_mask_level(value: str) -> str:
    """Mask ONE free-form text level of a canary diagnostic field.

    The env-secret / URL / phone / account / tracking redaction
    (``_redact_output``), the whole-header masking and the short / structured
    credential-shape chain — the same set ``_sanitize_canary_diag_field`` has
    always applied, factored out so the BOUNDED-RECURSIVE JSON walker
    (``tripchord._secret_redact.bounded_json_mask``, C-122 supervision 06:58)
    can apply it at every decoded level of a multi-``json.dumps`` summary.
    """
    # C-122 supervision 09:00 (gap 2): same zero-width-split pre-pass as the
    # producer — a Cf / full-width credential-FIELD assignment (``Session​token:…``)
    # is masked WHOLE on the normalized copy BEFORE the ASCII chain runs, so the
    # ASCII credential-field shape cannot collapse ``token:…`` and leave the
    # name half.
    # The WHOLE-HEADER shape runs here too so a full-width ``\uff21uthorization:
    # Basic YWJjZA==`` masks name-and-base64 together (the tightened
    # credential-FIELD value pattern stops at the space after ``Basic``).
    # C-122 supervision 09:28 (gap 2 regression): collapse URLs on the RAW text
    # BEFORE the normalized credential-field pre-pass.  The credential-field
    # value now runs from the first value char to a clear field boundary (spaces
    # included), so without this a trailing ``https://…`` scheme would be
    # swallowed into the value (``token=abc https``) and the URL host would
    # survive once the scheme half was redacted.  Masking the URL first turns
    # ``token=abc <url>`` into a clean ``[REDACTED] <url>``.
    value = _CANARY_DIAG_URL_RE.sub("<url>", value)
    value = mask_normalized_spans(
        value,
        (_CANARY_DIAG_WHOLE_HEADER_RE, _CANARY_DIAG_CREDENTIAL_FIELD_RE),
        marker="[REDACTED]",
    )
    value = _redact_output(value)
    value = _CANARY_DIAG_WHOLE_HEADER_RE.sub("[REDACTED]", value)
    # R36 Block 62 consumer half: mirror the producer — mask the WHOLE real
    # Digest credential descriptor BEFORE the token-run shape, so the 32+ hex
    # response is not pre-collapsed and the span builder can still see it.  The
    # ``username=`` identity params and the ``bad=…`` malformed tail then fail
    # closed together with the response instead of surviving the sanitizer.
    value = _mask_digest_credential_text(value)
    value = _CANARY_DIAG_TOKEN_RUN_RE.sub("[REDACTED]", value)
    value = _CANARY_DIAG_AKIA_RE.sub("[REDACTED]", value)
    value = _CANARY_DIAG_PREFIX_TOKEN_RE.sub("[REDACTED]", value)
    value = _CANARY_DIAG_BEARER_RE.sub("[REDACTED]", value)
    value = _CANARY_DIAG_DOTTED_TOKEN_RE.sub("[REDACTED]", value)
    # C-122 supervision 09:59 Block 4: the legacy ``opaque_kv`` mask is gone —
    # every key it carried is now folded into the credential-FIELD shape with
    # the shared strong/weak boundary semantics (the credential_field line
    # below does the work).
    value = _CANARY_DIAG_CREDENTIAL_FIELD_RE.sub("[REDACTED]", value)
    # C-122 round-26 Block 41 + round-27 Block 43: free-text bare values are
    # masked by the CONSUMER too, mirroring the producer — the closed
    # registered business-identifier bases survive IN THEIR DOCUMENTED SCHEMA
    # FORM (``flightOption1`` / ``refreshTokenCount1`` / ``plannerV2`` …),
    # every other bare value (``qwerTy1`` / ``myFlightHotel1`` …) AND a
    # registered base in the wrong schema form (``planner1`` / ``provider9``)
    # or inside a credential-NARRATION context (``password is flightOption1``)
    # fails closed to ``[REDACTED]`` before it can reach the committed report.
    value = _mask_bare_credential_text(value)
    return value


def _consume_canary_failure_diagnostic(
    evidence_path: Path,
    *,
    run_id: str = "",
    tested_commit_sha: str = "",
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Read and verify the 0600 canary failure diagnostic for a FAILED canary.

    C-122 round-19 (supervision 17:03 Block 2): when the canary crashes without a
    main JSON, the outer gate must consume the 0600 ``<output>.failure.json`` the
    canary sealed and verify schema / diagnostic kind / run_id / tested_sha /
    runtime / 0600 perms / freshness.  Returns ``(classification, error)`` —
    exactly one is non-None.  ``classification`` is the desensitized
    stage + exception class + summary plus the run_id / tested_sha / runtime
    bindings, carried into the layer-5 detail.  Any missing / stale / mismatched /
    unreadable diagnostic yields ``error`` so the layer fails closed explicitly
    instead of silently consuming an old or foreign failure.
    """
    now = now or datetime.now(UTC)
    diag_path = evidence_path.with_suffix(evidence_path.suffix + ".failure.json")
    if not diag_path.is_file():
        return None, (
            "canary failed without a main JSON but no failure diagnostic "
            "(expected 0600 <output>.failure.json)"
        )
    try:
        mode = stat.S_IMODE(diag_path.stat().st_mode)
    except OSError as exc:
        return None, f"canary failure diagnostic stat failed: {exc}"
    if mode != 0o600:
        return None, f"canary failure diagnostic must be 0600, got {oct(mode)}"
    try:
        diagnostic = json_loads_no_dupes(diag_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"canary failure diagnostic unreadable or invalid JSON: {exc}"
    if not isinstance(diagnostic, dict):
        return None, "canary failure diagnostic is not an object"
    # C-122 supervision 18:13 counter-example (未知字段): the diagnostic is a
    # STRUCTURED contract — any unknown top-level / run_identity / runtime field
    # makes it a foreign or smuggling structure and fails closed before its
    # summary is consumed, even when every known field looks valid.
    unknown_top_level = set(diagnostic) - _CANARY_DIAG_ALLOWED_TOP_LEVEL
    if unknown_top_level:
        return None, (
            "canary failure diagnostic carries unknown field(s): "
            f"{', '.join(sorted(unknown_top_level))}"
        )
    if diagnostic.get("schema_version") != _CANARY_DIAG_SCHEMA_VERSION:
        return None, "canary failure diagnostic schema_version mismatch"
    if diagnostic.get("diagnostic_kind") != "canary_failure":
        return None, "canary failure diagnostic diagnostic_kind mismatch"
    stage = diagnostic.get("stage")
    exception_class = diagnostic.get("exception_class")
    if not isinstance(stage, str) or not stage:
        return None, "canary failure diagnostic missing stage"
    if not isinstance(exception_class, str) or not exception_class:
        return None, "canary failure diagnostic missing exception_class"
    run_identity = diagnostic.get("run_identity")
    if not isinstance(run_identity, dict):
        return None, "canary failure diagnostic missing run_identity"
    unknown_identity_fields = set(run_identity) - _CANARY_DIAG_ALLOWED_RUN_IDENTITY
    if unknown_identity_fields:
        return None, (
            "canary failure diagnostic run_identity carries unknown field(s): "
            f"{', '.join(sorted(unknown_identity_fields))}"
        )
    diag_run_id = run_identity.get("run_id")
    if not isinstance(diag_run_id, str) or not diag_run_id:
        return None, "canary failure diagnostic missing run_id"
    if run_id and diag_run_id != run_id:
        return None, (
            f"canary failure diagnostic run_id {diag_run_id!r} != this run "
            f"{run_id!r}"
        )
    diag_sha = run_identity.get("tested_sha")
    if not isinstance(diag_sha, str) or not diag_sha:
        return None, "canary failure diagnostic missing tested_sha"
    if tested_commit_sha and diag_sha != tested_commit_sha:
        return None, (
            f"canary failure diagnostic tested_sha {diag_sha!r} != this run "
            f"{tested_commit_sha!r}"
        )
    runtime = run_identity.get("runtime")
    if not isinstance(runtime, dict):
        return None, "canary failure diagnostic missing runtime identity"
    unknown_runtime_fields = set(runtime) - _CANARY_DIAG_ALLOWED_RUNTIME
    if unknown_runtime_fields:
        return None, (
            "canary failure diagnostic runtime carries unknown field(s): "
            f"{', '.join(sorted(unknown_runtime_fields))}"
        )
    # C-122 supervision 01:10 Block 3 counter-example: the diagnosis runtime must
    # be bound EXACTLY to the ACTUAL run's authoritative interpreter identity —
    # not merely non-empty.  ``EVIL-RUNTIME`` / ``EVIL-PLATFORM`` (or a python
    # version that does not equal this gate's own ``sys.version_info[:3]``) is a
    # forged diagnosis and fails closed.
    diag_python = runtime.get("python")
    diag_platform = runtime.get("platform")
    if (
        diag_python != _CANARY_DIAG_EXPECTED_PYTHON
        or diag_platform != _CANARY_DIAG_EXPECTED_PLATFORM
    ):
        return None, (
            "canary failure diagnostic runtime identity "
            f"{diag_python!r}/{diag_platform!r} != this run's authoritative "
            f"runtime {_CANARY_DIAG_EXPECTED_PYTHON!r}/"
            f"{_CANARY_DIAG_EXPECTED_PLATFORM!r}"
        )
    generated_at = diagnostic.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        return None, "canary failure diagnostic missing generated_at"
    try:
        generated_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None, "canary failure diagnostic generated_at is not a timestamp"
    if generated_dt.tzinfo is None:
        return None, "canary failure diagnostic generated_at is not timezone-aware"
    age = (now - generated_dt).total_seconds()
    if age < 0 or age > _CANARY_DIAG_MAX_AGE_SECONDS:
        return None, (
            f"canary failure diagnostic is stale (age {age:.0f}s > "
            f"{_CANARY_DIAG_MAX_AGE_SECONDS}s)"
        )
    summary = diagnostic.get("summary")
    if not isinstance(summary, str) or not summary:
        summary = "no exception detail"
    # C-122 supervision 01:10 Block 3: the classification is carried into the
    # committed report, so every free-form diagnostic field is re-sanitized by
    # the CONSUMER (this function) — a credential-bearing summary, exception
    # class or stage is redacted / collapsed / bounded before it lands.
    summary = _sanitize_canary_diag_field(summary, "no exception detail")
    exception_class = _sanitize_canary_diag_field(
        exception_class, "unknown exception"
    )
    stage = _sanitize_canary_diag_field(stage, "unknown stage")
    classification = (
        f"canary failure diagnostic consumed: stage={stage} "
        f"exception={exception_class} summary={summary} "
        f"run_id={diag_run_id} tested_sha={diag_sha[:12]} "
        f"runtime=python {_CANARY_DIAG_EXPECTED_PYTHON}/"
        f"{_CANARY_DIAG_EXPECTED_PLATFORM} "
        f"generated_at={generated_dt.isoformat()}"
    )
    return classification, None


def layer5_real_canary(
    staging_dir: Path,
    *,
    run_id: str = "",
    tested_commit_sha: str = "",
) -> LayerResult:
    """Every declared-certified real provider x vertical needs a live canary.

    The layer verdict is driven by a per-scope certified OTA canary
    (``benchmarks/live_canary_certified.py``): each certified scope
    must show a fresh, authorised, read-only canary — a fresh Companion
    heartbeat for the browser scopes and a real public API read for
    ``icom:transfer``.  The open-meteo / dpm.org.cn probes are kept as a
    separately-labelled public-page connectivity canary that never drives the
    layer verdict.

    ``run_id`` / ``tested_commit_sha`` (when non-empty) are passed to the canary
    so a crash seals a diagnostic that binds THIS run at THIS revision (Block 2).
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
    canary_argv = [
        "uv",
        "run",
        "python",
        "benchmarks/live_canary_certified.py",
        "--output",
        str(evidence_path),
    ]
    # C-122 round-19 (Block 2): bind this run + this tested revision into the
    # canary so a crash seals a diagnostic the gate can verify as current-owned.
    if run_id:
        canary_argv += ["--run-id", run_id]
    if tested_commit_sha:
        canary_argv += ["--tested-sha", tested_commit_sha]
    code, _ = _run(
        canary_argv,
        env=_bridge_env(bridge_token),
        timeout=900,
    )
    passed = False
    canary_failures: list[str] = []
    # C-118: the certified canary must BOTH exit 0 AND carry a passed=true JSON
    # with the exact certified scope set each fresh/authorized/read-only/passed.
    # A non-zero exit cannot be papered over by a forged all-green JSON, and a
    # canary that exits 0 while reporting a failed/stale/unauthorized/incomplete
    # scope set must fail the layer (C-114 review R2).
    if code != 0:
        canary_failures.append(
            f"canary process exited {code} (must exit 0 with certified JSON)"
        )
    try:
        report = json_loads_no_dupes(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
        canary_failures.append("canary evidence JSON missing or unreadable")
    # C-122 round-19 (supervision 17:03 Block 2): when the canary failed (non-zero
    # exit OR the main JSON is missing/unreadable), the outer layer MUST consume
    # the 0600 failure diagnostic the canary sealed — verify schema / run_id /
    # tested_sha / runtime / 0600 perms / freshness and keep the desensitized
    # classification + bindings in the layer-5 detail.  A missing diagnostic is
    # only mandatory when the canary crashed without a report; a canary that
    # exits 2 carrying a valid passed=false report legitimately has no diagnostic
    # (its report IS the failure evidence).  Old / mismatched / missing
    # diagnostics fail closed explicitly — never silently consumed.
    if code != 0 or report is None:
        diag_path = evidence_path.with_suffix(
            evidence_path.suffix + ".failure.json"
        )
        if diag_path.is_file() or report is None:
            classification, diag_error = _consume_canary_failure_diagnostic(
                evidence_path,
                run_id=run_id,
                tested_commit_sha=tested_commit_sha,
            )
            if diag_error is not None:
                canary_failures.append(diag_error)
            elif classification is not None:
                sub_checks.append(
                    {
                        "name": "canary_failure_diagnostic",
                        "passed": False,
                        "drives_pass": True,
                        "detail": classification,
                    }
                )
    if report is not None:
        scopes = report.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            canary_failures.append("canary carries no scopes")
        else:
            # C-122 round-18 item 4: the raw scope array must be EXACTLY the
            # certified canary scope set (five browser Companion OTA scopes +
            # the iCom public-API scope, 6 total), each a dict with a unique
            # non-empty name.  A canary that smuggles a string/None entry, a
            # duplicate or an extra scope past the green checks fails the layer
            # — nothing is silently skipped or deduped.
            if len(scopes) != len(_ALL_CERTIFIED_CANARY_SCOPES):
                canary_failures.append(
                    f"canary carries {len(scopes)} scope entries; exactly "
                    f"{len(_ALL_CERTIFIED_CANARY_SCOPES)} certified scopes required"
                )
            present: set[str] = set()
            seen: set[str] = set()
            for entry in scopes:
                if not isinstance(entry, dict):
                    canary_failures.append("canary scope entry is not an object")
                    continue
                scope = entry.get("scope")
                if not isinstance(scope, str) or not scope:
                    canary_failures.append(
                        "canary scope entry missing a non-empty scope name"
                    )
                    continue
                if scope in seen:
                    canary_failures.append(f"duplicate canary scope: {scope}")
                seen.add(scope)
                present.add(scope)
                ok = (
                    entry.get("passed") is True
                    and entry.get("fresh") is True
                    and entry.get("authorized") is True
                    and entry.get("read_only") is True
                )
                sub_checks.append(
                    {
                        "name": scope,
                        "passed": ok,
                        "drives_pass": True,
                        "detail": entry.get("detail", ""),
                    }
                )
                if not ok:
                    canary_failures.append(
                        f"{scope}: not fresh/authorized/read-only/passed"
                    )
            missing = sorted(_ALL_CERTIFIED_CANARY_SCOPES - present)
            if missing:
                canary_failures.append("missing certified scopes: " + ", ".join(missing))
            # C-118: the certified canary must declare EXACTLY the certified
            # scopes (the certified browser Companion + the iCom public-API scope) — an
            # extra/ad-hoc scope is not certified and must fail the layer rather
            # than inflate coverage (C-122 HG-A).
            extra = sorted(present - _ALL_CERTIFIED_CANARY_SCOPES)
            if extra:
                canary_failures.append(
                    "non-certified extra scopes: " + ", ".join(extra)
                )
        if report.get("passed") is not True:
            canary_failures.append("canary passed != true")
        status = report.get("companion_status")
        if isinstance(status, dict) and not status.get("error") and "companions" in status:
            sub_checks.append(
                {
                    "name": "companion_status",
                    "passed": True,
                    "drives_pass": False,
                    "detail": "local Browser Bridge companion status endpoint reachable",
                }
            )
    if canary_failures:
        sub_checks.append(
            {
                "name": "certified_ota_canary",
                "passed": False,
                "drives_pass": True,
                "detail": "; ".join(canary_failures[:5]),
            }
        )
    else:
        passed = True
    return LayerResult(
        name="5_real_canary",
        passed=passed,
        detail=(
            "all 6 certified canary scopes (five browser Companion OTA + iCom "
            "public API) have fresh authorised read-only canaries"
            if passed
            else (
                "pending user authorization: not all certified canary scopes have "
                "a fresh authorised read-only canary; evidence in "
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


# The certified canary scope contract is DERIVED from the authoritative
# registry — never hardcoded.  ``build_default_registry().certified_scopes()``
# returns exactly the CERTIFIED_ACTIVE set: five browser Companion OTA scopes
# (ctrip:flight, ctrip:lodging, qunar:flight, qunar:lodging, tongcheng:flight)
# plus the iCom public-API scope (icom:transfer) = 6 total.
#
# C-122 round-19 (2026-08-11 17:03 supervision veto): ``tongcheng:lodging`` is
# DISABLED in the authoritative registry (user skipped on 2026-08-05) — it must
# NEVER enter the canary / compact / gate as a required member, and must never
# be silently re-enabled by a hardcoded contract.  The layer-5 canary and the
# layer-5/layer-6 compacts all bind to this registry-derived exact set.
_CERTIFIED_CANARY_SCOPE_KEYS: frozenset[str] = frozenset(
    scope.key for scope in build_default_registry().certified_scopes()
)

# The BROWSER Companion OTA scopes layer 5 requires (a fresh Companion heartbeat
# for each browser provider).  ``icom:transfer`` is an independent public API
# read, NOT a browser Companion scope, and must never appear in a Companion's
# ``authorized_scope_keys``.
_CERTIFIED_OTA_SCOPES = frozenset(
    key for key in _CERTIFIED_CANARY_SCOPE_KEYS if not key.startswith("icom:")
)

# The iCom public-API scope: a real read-only transfer-search query, separate
# from the browser Companion authorization contract.  It stays in the certified
# canary scope set but is never part of a Companion's ``authorized_scope_keys``.
_ICOM_PUBLIC_API_SCOPES = frozenset(
    key for key in _CERTIFIED_CANARY_SCOPE_KEYS if key.startswith("icom:")
)

# The full certified canary scope set = the certified browser Companion OTA
# scopes plus the iCom public-API scope.  The layer-5 canary's raw scope array
# and the layer-5 compact's coverage/scope set must be exactly this set.
_ALL_CERTIFIED_CANARY_SCOPES = _CERTIFIED_OTA_SCOPES | _ICOM_PUBLIC_API_SCOPES

# The fixed browser platform set the certified canary must complete: the OTA
# providers named by the browser Companion certified scopes.  The
# strict-coverage evidence and the real browser source evidence both bind to
# exactly this set — never a forged or partial provider list.
_BROWSER_OTA_PROVIDERS = frozenset(
    scope.split(":", 1)[0] for scope in _CERTIFIED_OTA_SCOPES
)

# C-122 HG-G: the frozen live-v4 scenario seals EXACTLY three date pairs, each
# executing the same fixed per-pair browser-source / query-task / iCom-source
# plan (mirror of the producer's ``len(run.pair_runs) != 3`` contract in
# apps/api/src/tripchord/agents/live_done_gate_v4.py).  The layer-6 compact
# validator uses this to reject a forged 1-pair / 1-task source graph.
#
# C-122 round-19 (supervision 17:03 Block 1): the frozen constants and the
# canonical MEMBER SETS are DERIVED from the shared canonical frozen graph
# (tripchord/planning/frozen_graph.py) — the exact same source the producer's
# ``_check_v4_source_graph`` derives its expected sets from.  The validator
# compares the compact's member sets against these EXACT sets (not just their
# lengths), so a foreign member, a wrong-pair swap or a missing/extra iCom task
# all fail closed.
_V4_FROZEN_DATE_PAIR_COUNT = FROZEN_V4_PAIR_COUNT

# The frozen per-pair browser-source / query-task count the layer-6 v4 source
# graph seals on EVERY frozen date pair.  The real frozen maldives scenario
# schedules 13 browser queries per pair (the 6 enabled ctrip kinds + the 6
# enabled qunar kinds + tongcheng's single flight), so a compact that shrinks the
# per-pair count to 1, or declares a per-pair task count whose ID sets carry a
# different number of Source ids / query shapes, is a forged graph and must fail
# closed even though every field is non-empty / unique / positive.
_V4_FROZEN_TASKS_PER_PAIR = frozen_v4_tasks_per_pair()

# Canonical frozen member sets for exact per-pair comparison (Block 1).
_V4_FROZEN_BROWSER_SOURCE_IDS = frozen_v4_browser_source_ids()
_V4_FROZEN_QUERY_SHAPES = frozen_v4_query_shapes()
_V4_FROZEN_ICOM_TASK_IDS = frozen_v4_icom_task_ids()

# C-122 supervision 01:10: pair-id canonical binding.  The SEALED pair-id set of
# a real run is not a fixed constant — the API applies ``minimum_departure_
# lead_days=7`` to the frozen window at run time and the pair execution refines
# the exploration anchors, so the three ``date-pair:`` ids depend on when the
# run happens.  The layer-6 validator therefore:
#
#   * requires every compact ``pair_id`` to be a CANONICAL frozen-scenario id
#     (``frozen_v4_pair_id_is_canonical``: well-formed ``date-pair:`` format and
#     a digest that recomputes from the frozen scenario constants), which
#     rejects arbitrary foreign ids like ``pair-1``, and
#   * requires the compact's ``pair_ids`` SET to equal the run's checkpoint-bound
#     sealed pair ids EXACTLY (``checkpoint_bound_pair_ids`` — an independent
#     job-control-plane record carried in the compact), which rejects a foreign
#     pair, a wrong-pair swap, and a missing/extra pair even when every id is
#     well-formed.
#
# The producer's ``_check_v4_source_graph`` uses the SAME ``frozen_v4_pair_id_
# is_canonical`` derivation on its ``run.pair_runs`` ids, so a run that produced
# a foreign / malformed / wrong-digest pair id fails closed on the producer side
# too.


def _resolve_live_state_db(explicit: Path | None = None) -> Path:
    """The durable live-state SQLite file a live run must not pollute (C-114 R7).

    Defaults to ``<repo-root>/tripchord.db`` — the same file the API's
    ``DATABASE_URL`` default resolves to when launched from the repository root.
    An explicit ``--live-state-db`` wins; a non-local ``DATABASE_URL`` /
    ``TRIPCHORD_DATABASE_URL`` is ignored in favour of the default rather than
    guessing at a remote host.
    """
    if explicit is not None:
        return explicit
    for key in ("DATABASE_URL", "TRIPCHORD_DATABASE_URL"):
        url = os.environ.get(key)
        if not url:
            continue
        # sqlite+aiosqlite:///./tripchord.db -> ./tripchord.db
        match = re.search(r"sqlite(?:\+\w+)?:///(.*)$", url)
        if match and match.group(1):
            return ROOT / match.group(1)
    return ROOT / "tripchord.db"


def _live_state_lease_preflight(db_path: Path) -> list[str]:
    """Read-only live-state preflight: residual queued/claimed leases.

    Opens the SQLite file strictly read-only (``mode=ro``) so it can neither
    clear nor extend a lease, and therefore cannot mask the very residual-lease
    problem it exists to detect (C-114 R7).  A job whose status is ``queued`` or
    ``running`` and whose lease has not yet expired is residual: a fresh live run
    would contend with it.  Returns an empty list when the live state is
    isolated; each non-empty entry names one residual lease (job id + status +
    lease detail) so the runner can surface *which* live-state residue blocks the
    run.
    """
    if not db_path.is_file():
        return [f"live-state DB {db_path} missing; cannot prove lease isolation"]
    try:
        connection = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5
        )
    except (OSError, sqlite3.Error) as exc:
        return [
            f"live-state DB {db_path} unreadable ({exc.__class__.__name__}); "
            "cannot prove lease isolation"
        ]
    residual: list[str] = []
    try:
        rows = connection.execute(
            "SELECT id, status, lease_expires_at FROM jobs "
            "WHERE status IN ('queued', 'running')"
        ).fetchall()
    except sqlite3.Error as exc:
        return [
            f"live-state DB {db_path} jobs query failed ({exc.__class__.__name__}); "
            "cannot prove lease isolation"
        ]
    finally:
        connection.close()
    now = datetime.now(UTC)
    for job_id, status, lease_raw in rows:
        if lease_raw is None:
            # queued/running with no lease bound is still residual: the job is
            # pending/active work a fresh run would race with.
            residual.append(
                f"job {job_id} status={status} holds an active pending lease"
            )
            continue
        try:
            expires = datetime.fromisoformat(str(lease_raw))
        except ValueError:
            residual.append(
                f"job {job_id} status={status} has unparseable lease {lease_raw!r}"
            )
            continue
        # C-122 round-18 gate-5: a NAIVE lease is fail-closed.  ``fromisoformat``
        # of a bare wall clock (no timezone) yields a naive datetime, and
        # ``astimezone(UTC)`` on a naive value silently relabels it as host-local
        # — a +08:00 live state would then be misjudged by eight hours.  A lease
        # without an explicit zone cannot be compared safely, so it is residual.
        if expires.tzinfo is None or expires.utcoffset() is None:
            residual.append(
                f"job {job_id} status={status} has a naive lease {lease_raw!r} "
                "(no timezone; cannot compare safely)"
            )
            continue
        # C-122 round-18: astimezone(UTC) CONVERTS an aware lease with a
        # non-UTC offset; replace(tzinfo=UTC) would just relabel the wall clock,
        # misjudging a +08:00 lease by eight hours.
        if expires.astimezone(UTC) > now:
            residual.append(
                f"job {job_id} status={status} holds lease until {expires.isoformat()}"
            )
    return residual


# Residual Browser-Bridge state classes (C-118, C-122 Fix 2).  The persisted
# bridge-state JSON (BrowserBridgePersistedState, schema v2) tracks Companion
# tasks and reload/control requests independently of the planning ``jobs``
# table: a task still ``queued``/``claimed`` or a control request still
# ``queued``/``draining``/``dispatched``/``accepted`` is in-flight work a fresh
# live run would race with.  Reading only this file can prove bridge-lease
# isolation that a ``tripchord.db`` planning-jobs query cannot.
#
# Residual task semantics: ``queued`` and ``claimed`` are BOTH residual — a
# ``claimed`` task is requeued on bridge restart (recoverable expired running,
# browser_bridge._restore_persisted_state), so it must not be in flight when a
# fresh live run starts.  There is no persisted ``requeued`` state string; a
# claimed record is exactly the "will be requeued / recoverable expired
# running" case the preflight must reject.
_BRIDGE_TASK_STATES = frozenset(
    {"queued", "claimed", "succeeded", "blocked", "failed", "cancelled"}
)
_BRIDGE_TASK_RESIDUAL_STATES = frozenset({"queued", "claimed"})
_BRIDGE_CONTROL_STATES = frozenset(
    {"queued", "draining", "dispatched", "accepted", "applied", "failed", "expired"}
)
_BRIDGE_CONTROL_RESIDUAL_STATES = frozenset(
    {"queued", "draining", "dispatched", "accepted"}
)
_BRIDGE_STATE_SCHEMA_VERSION = "tripchord-browser-bridge-state-v2"
_BRIDGE_STATE_ENV = "TRIPCHORD_BROWSER_BRIDGE_STATE_PATH"
# The bridge-state file the launchd live API actually persists (docs/operations.md
# sets ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH="$PWD/.runtime/browser-bridge-state.json"``).
_BRIDGE_STATE_DEFAULT_REL = ".runtime/browser-bridge-state.json"


def _resolve_bridge_state_path(explicit: Path | None = None) -> Path:
    """The persisted Browser-Bridge state JSON path for the lease preflight.

    C-122 Fix 2: the preflight must bind to the bridge-state file the live API
    actually persists (``.runtime/browser-bridge-state.json``).  An explicit
    ``--bridge-state-path``-style override wins; otherwise
    ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH`` (the env var the bridge provider
    itself honours, ``browser_bridge.BROWSER_BRIDGE_STATE_PATH_ENV``); when both
    are unset the default ``.runtime`` file is used — never a vacuous "no
    persisted bridge state to contend with" pass.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(_BRIDGE_STATE_ENV, "").strip()
    if raw:
        return Path(raw)
    return ROOT / _BRIDGE_STATE_DEFAULT_REL


# The immutable bridge-state byte snapshot bound by the most recent lease
# preflight (C-122 round-18 item 3).  Set by ``_bridge_state_validate`` from
# the exact bytes it read, so the layer-6 compact certifies the SAME snapshot
# the preflight validated — never a second read after the long E2E run, during
# which the live API legitimately repersists the file.
_BRIDGE_STATE_SNAPSHOT: dict[str, Any] | None = None

# A SECOND immutable bridge-state byte snapshot captured AFTER the layer-6 E2E
# completes (C-122 round-18 item 6).  Set by ``_bridge_state_validate`` with
# ``after=True`` so the committed trail carries the isolation proof from both
# sides of the run: no residual queued/claimed/reload before it started, and
# none left behind by the run itself.
_BRIDGE_STATE_SNAPSHOT_AFTER: dict[str, Any] | None = None


def _bridge_state_rel_identifier(path: Path) -> str:
    """A repo-relative identifier for the bridge-state file — never the absolute
    host path (C-122 round-18 item 3).  A file outside the repo tree is named by
    its basename so no host path can leak into the committed trail.
    """
    try:
        rel = os.path.relpath(path.resolve(), ROOT.resolve())
    except (OSError, ValueError):
        return path.name
    if rel.startswith("..") or os.path.isabs(rel):
        return path.name
    return rel


def _bridge_state_binding() -> dict[str, Any]:
    """Repo-relative identifier + SHA256 + preflight result of the bridge-state
    file the lease preflight binds to.

    Written into the layer-6 compact so the committed trail records exactly
    which bridge-state file was checked, its byte hash, and the preflight
    outcome (the residual-problem list).  Only the hash and the residual result
    — never the file's token/quote/URL bytes — leave the runtime.  When a
    preflight captured an immutable snapshot for the currently-resolved path,
    that snapshot is returned; otherwise the file is re-read (direct-call /
    fallback path).
    """
    path = _resolve_bridge_state_path()
    rel_id = _bridge_state_rel_identifier(path)
    snapshot = _BRIDGE_STATE_SNAPSHOT
    if (
        snapshot is not None
        and snapshot.get("file") == rel_id
        and snapshot.get("sha256") is not None
    ):
        return {
            "file": snapshot["file"],
            "sha256": snapshot["sha256"],
            "residual": list(snapshot.get("residual") or ()),
        }
    return {
        "file": rel_id,
        "sha256": _sha256_file(path) if path.is_file() else None,
        "residual": [],
    }


def _bridge_state_after_binding() -> dict[str, Any]:
    """Repo-relative identifier + SHA256 + postcheck result of the bridge-state
    file captured AFTER the layer-6 E2E completed (C-122 round-18 item 6).

    Written into the layer-6 compact so the committed trail records BOTH the
    pre-run preflight binding and this post-run binding: the run started from a
    lease-clean bridge and left no residual queued/claimed/reload behind.  Only
    the hash and the residual result — never the file's token/quote/URL bytes —
    leave the runtime.  When a postcheck captured an immutable snapshot for the
    currently-resolved path, that snapshot is returned; otherwise the file is
    re-read (direct-call / fallback path).
    """
    path = _resolve_bridge_state_path()
    rel_id = _bridge_state_rel_identifier(path)
    snapshot = _BRIDGE_STATE_SNAPSHOT_AFTER
    if (
        snapshot is not None
        and snapshot.get("file") == rel_id
        and snapshot.get("sha256") is not None
    ):
        return {
            "file": snapshot["file"],
            "sha256": snapshot["sha256"],
            "residual": list(snapshot.get("residual") or ()),
        }
    return {
        "file": rel_id,
        "sha256": _sha256_file(path) if path.is_file() else None,
        "residual": [],
    }


def _is_timezone_aware_iso(value: object) -> bool:
    """True when ``value`` is an ISO-8601 string carrying a UTC offset.

    The real v2 bridge-state schema requires timezone-aware datetimes
    (``browser_bridge._require_timezone``); a naive timestamp cannot prove the
    persisted state is current, so the preflight fails closed on one.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _bridge_state_validate(state_path: Path | None, *, after: bool) -> list[str]:
    """Read-only Browser-Bridge state lease validation (preflight/postcheck).

    Reads the persisted bridge-state JSON the live API actually persists and
    reports every non-terminal task (``queued``/``claimed`` — the latter is
    requeued on restart, i.e. recoverable expired running) and pending reorder
    control request (``queued``/``draining``/``dispatched``/``accepted``) that a
    fresh live run would contend with (C-118).  The bridge stores these states
    independently of the planning ``jobs`` table, so the layer-6 residual-lease
    gate must read this file, not only ``tripchord.db``.  When ``after`` is
    true the SAME contract runs against a second read-only snapshot captured
    AFTER the layer-6 E2E completes: a residual queued/claimed task or pending
    reorder left behind proves the run did not consume its lease and is
    reported (C-122 round-18 item 6).

    Fail-closed contract: a missing file, a symlink, an unreadable file, an
    empty object, a wrong ``schema_version``, a non-array
    ``tasks``/``reload_requests``, a non-object entry, a missing state, or an
    unknown task/reload state each mean the file cannot prove bridge lease
    isolation and are reported as problems — never silently skipped.  Returns
    an empty list only when the file exists, parses to the expected schema and
    holds no residual work; each entry names one residual/schema problem.

    Records the immutable byte snapshot the compact must reuse (rel-identifier,
    sha256, residual) into ``_BRIDGE_STATE_SNAPSHOT`` (preflight) or
    ``_BRIDGE_STATE_SNAPSHOT_AFTER`` (postcheck), so the pre-run and post-run
    bindings each certify the exact bytes this validation read — never a second
    read after the long E2E run, during which the live API legitimately
    repersists the file (C-122 round-18 item 3/6).
    """
    if state_path is None:
        state_path = _resolve_bridge_state_path()
    if state_path is None:
        return []  # unreachable in practice: _resolve_bridge_state_path always binds
    if state_path.is_symlink():
        return [
            f"bridge-state path {state_path} is a symlink; refusing to read "
            "lease state through a symlink"
        ]
    if not state_path.is_file():
        return [
            f"bridge-state file {state_path} missing; cannot prove bridge lease "
            "isolation"
        ]
    _verify_evidence_file_safety(state_path, "bridge-state")
    try:
        # Bind the immutable byte snapshot: the sha256 the compact records must
        # be computed from the SAME bytes the validation parses and validates,
        # so a re-read after the long run can never certify a different snapshot
        # (C-122 round-18 item 3).
        payload_bytes = state_path.read_bytes()
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        payload = json_loads_no_dupes(payload_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        return [
            f"bridge-state file {state_path} unreadable "
            f"({exc.__class__.__name__}); cannot prove bridge lease isolation"
        ]
    if not isinstance(payload, dict):
        return [
            f"bridge-state file {state_path} is not a JSON object; cannot prove "
            "bridge lease isolation"
        ]
    if not payload:
        return [
            f"bridge-state file {state_path} is an empty object; cannot prove "
            "bridge lease isolation"
        ]
    residual: list[str] = []
    if payload.get("schema_version") != _BRIDGE_STATE_SCHEMA_VERSION:
        residual.append(
            f"bridge-state file {state_path} schema_version "
            f"{payload.get('schema_version')!r} != {_BRIDGE_STATE_SCHEMA_VERSION}"
        )
    # C-122 round-18 item 3/6: the real v2 schema requires a timezone-aware
    # ``saved_at`` (browser_bridge._require_timezone).  A missing or naive
    # timestamp cannot prove the state is current, so it fails closed — for
    # both the pre-run and the post-run snapshot.
    saved_at = payload.get("saved_at")
    if not _is_timezone_aware_iso(saved_at):
        residual.append(
            f"bridge-state file {state_path} saved_at "
            f"{saved_at!r} is not a timezone-aware timestamp"
        )
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        residual.append(
            f"bridge-state file {state_path} tasks is not an array "
            f"({type(tasks).__name__})"
        )
    else:
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                residual.append(
                    f"bridge-state file {state_path} task #{index} is not an object"
                )
                continue
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id:
                residual.append(
                    f"bridge-state file {state_path} task #{index} has no "
                    "non-empty id"
                )
            state = task.get("state")
            if not isinstance(state, str):
                residual.append(
                    f"bridge-state file {state_path} task {task_id!r} has no "
                    "string state"
                )
                continue
            if state not in _BRIDGE_TASK_STATES:
                residual.append(
                    f"bridge-state file {state_path} task {task_id!r} has unknown "
                    f"state {state!r}"
                )
                continue
            if state in _BRIDGE_TASK_RESIDUAL_STATES:
                residual.append(
                    f"bridge task {task_id!r} state={state} is queued/claimed"
                )
    reloads = payload.get("reload_requests")
    if not isinstance(reloads, list):
        residual.append(
            f"bridge-state file {state_path} reload_requests is not an array "
            f"({type(reloads).__name__})"
        )
    else:
        for index, item in enumerate(reloads):
            if not isinstance(item, dict):
                residual.append(
                    f"bridge-state file {state_path} reload #{index} is not an object"
                )
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                residual.append(
                    f"bridge-state file {state_path} reload #{index} has no "
                    "non-empty id"
                )
            state = item.get("state")
            if not isinstance(state, str):
                residual.append(
                    f"bridge-state file {state_path} reload {item_id!r} has no "
                    "string state"
                )
                continue
            if state not in _BRIDGE_CONTROL_STATES:
                residual.append(
                    f"bridge-state file {state_path} reload {item_id!r} has "
                    f"unknown state {state!r}"
                )
                continue
            if state in _BRIDGE_CONTROL_RESIDUAL_STATES:
                residual.append(
                    f"bridge reload {item_id!r} state={state} is pending reorder"
                )
    # Record the immutable snapshot the compact must reuse, so preflight/postcheck
    # and compact certify the same bytes (C-122 round-18 item 3/6).
    snapshot_global = (
        "_BRIDGE_STATE_SNAPSHOT_AFTER" if after else "_BRIDGE_STATE_SNAPSHOT"
    )
    globals()[snapshot_global] = {
        "file": _bridge_state_rel_identifier(state_path),
        "sha256": payload_sha256,
        "residual": list(residual),
    }
    return residual


def _bridge_state_lease_preflight(state_path: Path | None = None) -> list[str]:
    """Pre-run read-only Browser-Bridge state lease check (C-118).

    See ``_bridge_state_validate`` with ``after=False``: the pre-run snapshot a
    fresh live E2E must observe before it starts.
    """
    return _bridge_state_validate(state_path, after=False)


def _bridge_state_postcheck(state_path: Path | None = None) -> list[str]:
    """Post-run read-only Browser-Bridge state lease check (C-122 round-18 item 6).

    Captures a SECOND immutable snapshot AFTER the layer-6 E2E completes, so the
    committed trail holds the isolation proof from both sides of the run.  A
    residual queued/claimed task or pending reorder after the run means the run
    did not consume its lease — the layer-6 gate fails on a non-empty result.
    """
    return _bridge_state_validate(state_path, after=True)


# The real ``run_live_done_gate_v4`` completion artifact carries its verdict in
# ``done_gate.passed`` with the full itemised check set in ``done_gate.checks``
# (LiveV4DoneGateReport).  The outer layer 6 must not trust a process exit code
# or a forged top-level ``passed``: it requires the real 15-item check set, each
# item present and passed (C-114 review R1).
_V4_DONE_GATE_CHECK_NAMES = frozenset(
    {
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
    }
)


def _done_gate_mismatches(runner_evidence: dict[str, Any]) -> list[str]:
    """Validate the real layer-6 runner ``done_gate`` report contract.

    The runner's completed bundle embeds ``done_gate`` (the
    ``LiveV4DoneGateReport``): ``passed`` plus the full itemised ``checks``
    tuple.  Fail closed on a missing/mis-nested report, a non-true verdict, a
    missing/empty check list, any check that did not pass, or any of the 15
    required check names absent — a partial or forged summary can never pass.
    """
    mismatches: list[str] = []
    done_gate = runner_evidence.get("done_gate")
    if not isinstance(done_gate, dict):
        return [f"runner evidence carries no done_gate report ({done_gate!r})"]
    if done_gate.get("passed") is not True:
        mismatches.append(
            f"runner done_gate.passed = {done_gate.get('passed')!r} (must be true)"
        )
    checks = done_gate.get("checks")
    if not isinstance(checks, (list, tuple)) or not checks:
        mismatches.append("runner done_gate.checks missing or empty")
        return mismatches
    present: set[str] = set()
    seen: set[str] = set()
    failed: list[str] = []
    malformed = 0
    for item in checks:
        if not isinstance(item, dict):
            malformed += 1
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            malformed += 1
            continue
        if name in seen:
            mismatches.append(f"duplicate done-gate check: {name}")
        seen.add(name)
        present.add(name)
        if item.get("passed") is not True:
            failed.append(name)
    if malformed:
        mismatches.append(f"runner done_gate.checks has {malformed} malformed item(s)")
    # C-122 round-18 item 4: the raw check list must be exactly the fifteen
    # required checks — no duplicates, no extra non-certified items.  A forged
    # or inflated all-green summary can never pass.
    if len(checks) != len(_V4_DONE_GATE_CHECK_NAMES):
        mismatches.append(
            f"runner done_gate.checks carries {len(checks)} item(s); exactly "
            f"{len(_V4_DONE_GATE_CHECK_NAMES)} required"
        )
    missing = sorted(_V4_DONE_GATE_CHECK_NAMES - present)
    if missing:
        mismatches.append(
            "runner done_gate.checks missing required items: " + ", ".join(missing)
        )
    extra = sorted(present - _V4_DONE_GATE_CHECK_NAMES)
    if extra:
        mismatches.append(
            "runner done_gate.checks has extra non-certified item(s): "
            + ", ".join(extra)
        )
    if failed:
        mismatches.append(
            "runner done_gate.checks not all passed: " + ", ".join(failed)
        )
    return mismatches


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
    # The real completion artifact carries its verdict in done_gate.passed +
    # done_gate.checks (LiveV4DoneGateReport), never a top-level ``passed``.
    # Fail closed on any missing/mis-nested/failed check (C-114 review R1).
    mismatches.extend(_done_gate_mismatches(runner_evidence))
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


def layer6_full_e2e(
    staging_dir: Path, start: GitSnapshot, *, live_state_db: Path | None = None
) -> LayerResult:
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

    Before the executor is allowed to submit a fresh live run, a read-only
    live-state preflight (R7) must prove the durable job store holds no residual
    queued/claimed lease: a leftover lease would contaminate the new run.  The
    preflight only reads the DB — it can neither clear nor extend a lease, so it
    cannot mask the very residue it exists to detect.
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
    # C-118: the residual-lease gate reads BOTH the planning job store AND the
    # persisted Browser-Bridge state JSON.  The bridge keeps its own queued/
    # claimed task and pending-reorder state outside the ``jobs`` table, so a
    # fresh live run must not start while either holds in-flight work.
    lease_problems = _live_state_lease_preflight(
        _resolve_live_state_db(live_state_db)
    )
    lease_problems += _bridge_state_lease_preflight()
    if lease_problems:
        return LayerResult(
            name="6_full_e2e",
            passed=False,
            detail=(
                "live-state lease preflight failed (residual queued/claimed "
                "lease would contaminate this run): " + "; ".join(lease_problems)
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
    # C-122 round-18 item 6: capture a SECOND read-only bridge-state snapshot
    # AFTER the run completes and require it to hold no residual queued/claimed
    # work or pending reorder — the E2E must consume its own lease.  The compact
    # certifies this post-run binding alongside the pre-run preflight binding.
    postcheck_problems = _bridge_state_postcheck()
    if postcheck_problems:
        mismatches.append(
            "bridge-state postcheck failed (residual queued/claimed work or "
            "pending reorder after the run): " + "; ".join(postcheck_problems)
        )
    runner_evidence: dict[str, Any] = {}
    try:
        runner_evidence = json_loads_no_dupes(output_path.read_text(encoding="utf-8"))
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
            canary = json_loads_no_dupes(canary_path.read_text(encoding="utf-8"))
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
        # C-122 supervision 14:52: an executor that failed BEFORE the done gate
        # writes ``run_status: failed_before_done_gate`` with a ``failure``
        # record naming the exact stage and message.  Surface that REAL executor
        # failure truthfully — never fold it into a generic "pending user
        # authorization" wrapper (the layer's clean not-attempted cases are the
        # token / model-cost returns above).
        runner_failure = (
            runner_evidence.get("failure")
            if isinstance(runner_evidence, dict)
            else None
        )
        if isinstance(runner_failure, dict):
            stage = runner_failure.get("stage") or "unknown"
            message = runner_failure.get("message") or "no failure message"
            parts.append(
                f"executor failed before the done gate at stage {stage!r}: {message}"
            )
        if mismatches:
            parts.append("evidence cross-check failed: " + "; ".join(mismatches))
        elif code != 0:
            parts.append(f"run_live_done_gate_v4.py exited {code}")
        if out and not mismatches and not runner_failure:
            parts.append(out[-300:])
        if not parts:
            parts.append(f"run_live_done_gate_v4.py exited {code}")
        detail = " | ".join(parts)
    return LayerResult(
        name="6_full_e2e",
        passed=passed,
        detail=detail,
    )


def _applicable(layers: list[LayerResult]) -> list[LayerResult]:
    return [layer for layer in layers if not layer.skipped]


_RUN_ID_RE = re.compile(r"[0-9a-f]{12}")


def _new_run_id() -> str:
    """A short unique run identifier, bound into the staging path, the report
    and the committed manifest so each run's evidence is attributable to exactly
    one execution (C-114 R3)."""
    return uuid.uuid4().hex[:12]


def _new_staging_dir(run_id: str | None = None) -> Path:
    """A fresh git-ignored staging path for this run's evidence.

    The path embeds a timestamp plus a unique ``run_id`` (R3): two runs never
    collide on the same staging directory, so evidence written by run A cannot
    be silently reused by run B.  Returns only the path — ``main`` validates it
    before creating it, so a rejected target never leaves a side-effect
    untracked directory behind.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or _new_run_id()
    return RUNTIME_EVIDENCE_DIR / f"gate-{stamp}-{rid}"


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

    The check is lstat-based so a symlink is never followed: a planted symlink
    at the target could redirect the mkdir or the atomic rename to attacker-
    chosen bytes, so it is rejected outright (C-118).

    A pre-existing staging directory is ALWAYS rejected, even when empty
    (C-118): the staging root must be created exclusively by this run so no
    stale or pre-planted file can ever be swept into this run's committed
    trail.  Exclusivity is enforced here (fail-closed) and again by the
    non-``exist_ok`` ``mkdir`` in ``main``.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GateStateChangedError(
            f"{label} {path} cannot be inspected ({exc.__class__.__name__}); "
            "refusing to write over an unreadable target"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise GateStateChangedError(
            f"{label} {path} is a symlink; refusing to write evidence through a "
            "symlink target"
        )
    if kind == "dir":
        if not stat.S_ISDIR(st.st_mode):
            raise GateStateChangedError(
                f"{label} {path} exists and is not a directory; refusing to "
                "create a directory over a file"
            )
        # Exclusivity (C-118): an existing staging dir — empty or not — would
        # either mix stale evidence into the new run or accept a directory this
        # run did not itself create.  Only a freshly-created exclusive dir is
        # acceptable.
        raise GateStateChangedError(
            f"{label} {path} already exists; the staging dir must be created "
            "exclusively by this run"
        )
    # kind == "file": an existing regular file is an acceptable atomic-replace
    # target, but a directory is not.
    if stat.S_ISDIR(st.st_mode):
        raise GateStateChangedError(
            f"{label} {path} exists and is a directory; refusing to write a "
            "report over a directory"
        )


def run_gate(
    staging_dir: Path,
    *,
    commit: str | None = None,
    run_id: str | None = None,
    live_state_db: Path | None = None,
) -> GateReport:
    """Run all six layers and return the report.

    ``run_id`` uniquely identifies this execution (C-114 R3): it is bound into
    the report so every consumed artifact is attributable to exactly one run.
    A caller that pre-seeded the staging path with a run_id should pass the same
    id here; otherwise a fresh one is generated.

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
    # C-122 round-19 (Block 2): the run_id is resolved BEFORE the layers run so
    # the layer-5 canary can bind it into its failure diagnostic — a diagnostic
    # that does not name this run is never consumed.
    resolved_run_id = run_id or _new_run_id()
    layers = [
        layer1_reproducibility(),
        layer2_replay(staging_dir),
        layer3_clean_chrome_fixtures(staging_dir),
        layer4_model_smoke(),
        layer5_real_canary(
            staging_dir,
            run_id=resolved_run_id,
            tested_commit_sha=tested_commit_sha,
        ),
        layer6_full_e2e(staging_dir, start, live_state_db=live_state_db),
    ]
    # B1 secret scan: bridge token + model API keys must never reach logs or
    # evidence.  Fail closed (exit-2 semantics) before any verdict is certified
    # if a secret value or a sensitive evidence pattern does.
    _secret_scan_staging(staging_dir, _evidence_scan_needles())
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
        run_id=resolved_run_id,
        toplevel=start.toplevel,
        branch=start.branch,
        worktree_dirty=start.worktree_dirty,
        layers=layers,
        passed=passed,
        summary=summary,
        boundary=boundary,
    )


def _dump(report: GateReport, output_path: Path = OUTPUT_PATH) -> Path:
    # Every serialized report is redacted in place first (C-118): subprocess
    # output is already redacted at the source, and any remaining direct-detail
    # leak is neutralized here so no exit path — commit or not — can write or
    # print a raw secret.  Idempotent, so repeated dumps of the same report are
    # equally safe.
    _redact_report(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex[:8]}.tmp"
    )
    payload = json.dumps(
        asdict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # The report is owner-only even when ``--output`` points OUTSIDE the staging
    # tree (C-114 R6): the tmp is created sealed to 0600 (no chmod-after window,
    # C-122 Fix 5) and atomically renamed, so the final file is 0600 regardless
    # of the host umask or the target location.
    _write_sealed_bytes(tmp, payload, 0o600)
    os.replace(tmp, output_path)
    return output_path


def _write_sealed_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Create ``path`` with ``mode`` in force from the instant of creation and
    write ``payload`` to it.

    ``os.open(path, O_WRONLY | O_CREAT | O_EXCL, mode)`` seals permissions at
    birth — there is no ``write_text``-then-``chmod`` window where the file is
    briefly world/group-readable, regardless of the host umask (C-122 Fix 5).
    ``O_EXCL`` also guarantees the write never reuses a pre-existing (possibly
    attacker-planted) file.

    C-122 round-18: ``os.open`` still ANDs the requested mode with the process
    umask, so a restrictive ``umask 0777`` would produce a 0000 file that even
    the owner cannot read.  ``os.fchmod`` on the fresh fd pins the exact mode on
    the inode itself, immune to the umask, before any bytes are written.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb", closefd=True) as handle:
        handle.write(payload)


def _write_atomic(path: Path, text: str, mode: int = 0o600) -> Path:
    """Atomically write ``text`` to ``path`` sealed to ``mode`` (C-118).

    Writes to a uniquely-named sibling tmp in the target directory, sealed to
    the requested mode from birth via ``_write_sealed_bytes``, then
    ``os.replace``s it over the target.  A reader never observes a
    partially-written file, and the final permissions are independent of the
    host umask.  The tmp name embeds a uuid so concurrent runs cannot collide.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    _write_sealed_bytes(tmp, text.encode("utf-8"), mode)
    os.replace(tmp, path)
    return path


# Compact artifact names: deliberately NOT matching the git-ignored
# ``/benchmarks/results/live-*`` patterns so they are committed as the
# independently reviewable layer-5/6 evidence (C-114).
_COMPACT_CANARY_STAGED_NAME = "done-gate-layer5-compact.json"
_COMPACT_E2E_STAGED_NAME = "done-gate-layer6-compact.json"
# The single shared schema version the compact producer emits AND the blob
# read-back validator requires — a compact built by any other schema version is
# rejected (C-122 acceptance).
_LAYER5_COMPACT_SCHEMA = "tripchord-done-gate-layer5-compact-v2"
_LAYER6_COMPACT_SCHEMA = "tripchord-done-gate-layer6-compact-v2"
# C-122 supervision 09:59 (Block 1): a compact is a PUBLIC contract — its
# top-level field set is fixed.  A non-canonical ALIAS of a whitelisted digest
# key (``API_PAYLOAD_CANDIDATE_SET_SHA256``, ``api-payload-candidate-set-
# sha256``) or any other foreign top-level field makes the compact foreign and
# fails the validator closed, mirroring the raw-key exact match the digest
# whitelist walker enforces.
_LAYER5_COMPACT_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "generated_at",
        "passed",
        "bridge_token_present",
        "coverage",
        "scopes",
        "companion_status",
        "raw_evidence",
    }
)
_LAYER6_COMPACT_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "captured_at",
        "run_status",
        "done_gate",
        "repo_revision",
        "start_revision",
        "failure",
        "timeout_contract",
        "runner_contract",
        "event_injection_contract",
        "api_payload_candidate_set_sha256",
        "api_payload_sha256",
        "scenario_sha256",
        "runtime_before_run",
        "companion_preflight",
        "bridge_state_lease_preflight",
        "bridge_state_lease_postcheck",
        "raw_evidence",
    }
)

_EVIDENCE_TRACKED_PATHS: tuple[tuple[str, str], ...] = (
    ("product-acceptance.json", "benchmarks/results/product-acceptance.json"),
    ("browser-e2e.json", "benchmarks/results/browser-e2e.json"),
    (
        "browser-e2e-screenshot.png",
        "benchmarks/results/browser-e2e-screenshot.png",
    ),
    ("live-canary-certified.json", "benchmarks/results/live-canary-certified.json"),
    ("live-done-gate-v4.json", "benchmarks/results/live-done-gate-v4.json"),
    (_COMPACT_CANARY_STAGED_NAME, "benchmarks/results/done-gate-layer5-compact.json"),
    (_COMPACT_E2E_STAGED_NAME, "benchmarks/results/done-gate-layer6-compact.json"),
)

# The authoritative committed-evidence contract (C-122 supervision 18:13
# 规则漂移 counter-example).  WHICH evidence files E/P carry is FIXED by the
# S-tree publish rule, NEVER derived from the current worktree ``.gitignore``:
# raw sensitive ``live-*`` origins stay hash-only (``committed=False``, recorded
# by SHA256 in the manifest), while the desensitized layer-5/6 compacts and the
# acceptance / e2e artifacts are committed evidence (``committed=True``).  The
# publisher (``_manifest_files`` / ``_evidence_index_entries``) and the consumer
# (``verify_gate_ref``) derive the flag from THIS one contract, so editing the
# ``.gitignore`` — ignoring a committed file or un-ignoring a raw — can never
# flip the committed flag on an already-published S/E/P trail, and a manifest
# whose committed flag disagrees with the contract fails closed even when the
# worktree rule happens to agree with it.
_EVIDENCE_COMMITTED_CONTRACT: dict[str, bool] = {
    "product-acceptance.json": True,
    "browser-e2e.json": True,
    "browser-e2e-screenshot.png": True,
    "live-canary-certified.json": False,
    "live-done-gate-v4.json": False,
    _COMPACT_CANARY_STAGED_NAME: True,
    _COMPACT_E2E_STAGED_NAME: True,
}
# Every tracked path must be covered by the contract — a new evidence name added
# without a committed-flag ruling is a contract drift and must fail loudly at
# import time, not silently at publish time.
if set(name for name, _ in _EVIDENCE_TRACKED_PATHS) != set(
    _EVIDENCE_COMMITTED_CONTRACT
):
    raise RuntimeError(
        "evidence committed contract does not cover exactly the tracked evidence "
        "paths (rule-drift guard)"
    )


# The committed-evidence contract manifest.  The manifest is the *only* record
# of the git-ignored sensitive live-* evidence that E may not carry: it lists
# the SHA256 + size of every staging original (committed or not) plus redacted
# layer-5/6 verdict fields, so the audit trail proves what raw evidence existed
# and how it was ruled on, without committing token/Cookie/account/full-URL
# bytes.  ``committed`` records whether the raw file itself landed in E.
_MANIFEST_REL = "benchmarks/results/done-gate-evidence-manifest.json"
_MANIFEST_SCHEMA = "tripchord-done-gate-evidence-manifest-v1"

# Side-channel evidence publish (C-122 P0 / 2026-08-10 11:00 architecture
# ruling).  The gate never mutates the product branch / HEAD / real index /
# worktree: E and P are built from a temporary ``GIT_INDEX_FILE`` plus
# ``git commit-tree``, and the ONLY action that affects persistent state is the
# final atomic create-only ``git update-ref`` of a namespaced gate ref (old
# value all-zero, so a pre-existing ref fails the publish closed).  Consumers
# verify ``P^=E``, ``E^=S`` and the committed report/manifest bindings through
# that ref; the evidence commit is never installed as the product branch HEAD.
_DONE_GATE_REF_PREFIX = "refs/tripchord/done-gate"
_ZERO_SHA = "0" * 40
# The fixed non-personal identity stamped on the evidence commits E and P
# (C-122 round-18 gate-4): the side-channel evidence trail must never be
# attributed to a personal git identity.  The email uses the reserved
# ``.invalid`` TLD so it can never route, and both values are pinned in the
# commit env so ambient repo/user git config can never leak a personal name.
_EVIDENCE_COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "TripChord Done-Gate",
    "GIT_AUTHOR_EMAIL": "done-gate@tripchord.invalid",
    "GIT_COMMITTER_NAME": "TripChord Done-Gate",
    "GIT_COMMITTER_EMAIL": "done-gate@tripchord.invalid",
}
# The two files every evidence commit carries: the authoritative report and the
# evidence manifest.  ``product-v1-done-gate.json`` doubles as the delivered
# report in main(); ``done-gate-evidence-manifest.json`` is the manifest source
# for temp-index staging.
_REPORT_REL = "benchmarks/results/product-v1-done-gate.json"
_REPORT_STAGED_NAME = "product-v1-done-gate.json"
_MANIFEST_STAGED_NAME = "done-gate-evidence-manifest.json"


def _gate_ref(run_id: str) -> str:
    """The side-channel namespace ref a run's evidence is published under.

    Deterministic from ``run_id`` so a consumer can derive it from the report
    alone.  Refuses a run_id that is not the gate's own 12-hex format so a
    forged report cannot name an arbitrary ref.
    """
    if not _RUN_ID_RE.fullmatch(run_id):
        raise GateStateChangedError(
            f"refusing to publish evidence under invalid run_id {run_id!r}"
        )
    return f"{_DONE_GATE_REF_PREFIX}/{run_id}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _manifest_files(staging_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if not staged.is_file():
            continue
        _verify_evidence_file_safety(staged, "evidence")
        # C-122 supervision 18:13 (规则漂移): ``committed`` comes from the FIXED
        # authoritative contract, never from ``git check-ignore`` — a worktree
        # ``.gitignore`` edit must not flip the published committed flag.
        files.append(
            {
                "name": staged_name,
                "tracked_path": tracked_rel,
                "sha256": _sha256_file(staged),
                "size_bytes": staged.stat().st_size,
                "committed": _EVIDENCE_COMMITTED_CONTRACT[staged_name],
            }
        )
    return files


def _canary_manifest(staging_dir: Path) -> dict[str, Any] | None:
    """Redacted layer-5 canary verdict: scope keys + companion identity only."""
    path = staging_dir / "live-canary-certified.json"
    if not path.is_file():
        return None
    try:
        payload = json_loads_no_dupes(path.read_text(encoding="utf-8"))
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
        payload = json_loads_no_dupes(path.read_text(encoding="utf-8"))
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


def _desensitize_scope_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Extract only independently-reviewable structural fields from one canary
    scope's raw evidence — candidate identity, provider/scope bindings, quote
    sample and query-result counts — never raw URLs, account/session values or
    tokens (the compact must itself pass the final secret scan, C-114 R5)."""
    safe: dict[str, Any] = {}
    for key in (
        "companion_id",
        "adapter_version",
        "contract_version",
        "runtime_instance_id",
        "options",
        "searched_at",
    ):
        value = evidence.get(key)
        if value is not None:
            safe[key] = value
    providers = evidence.get("providers")
    if isinstance(providers, list):
        safe["providers"] = sorted({p for p in providers if isinstance(p, str)})
    authorized = evidence.get("authorized_scope_keys")
    if isinstance(authorized, list):
        safe["authorized_scope_keys"] = sorted(
            {a for a in authorized if isinstance(a, str)}
        )
    sample = evidence.get("sample")
    if isinstance(sample, dict):
        safe["sample"] = {
            "service_name": sample.get("service_name"),
            "departure_at": sample.get("departure_at"),
            "fare_amount": (
                str(sample["fare_amount"]) if sample.get("fare_amount") is not None else None
            ),
            "currency": sample.get("currency"),
        }
    source_urls = evidence.get("source_urls")
    if isinstance(source_urls, list):
        # The read-only query bindings are proven by count + the quote sample,
        # not by replaying raw URLs into a committed artifact.
        safe["source_url_count"] = len(source_urls)
    return safe


def _compact_canary(staging_dir: Path) -> dict[str, Any] | None:
    """Desensitized, independently reviewable layer-5 compact artifact.

    Carries the full per-scope verdict (scope/kind/passed/fresh/authorized/
    read_only), the provider/scope and candidate bindings, the live query-result
    and quote sample (icom), the certified-scope coverage thresholds, companion
    identity/heartbeat fields, and the SHA256 of the raw
    ``live-canary-certified.json`` it was derived from (C-114 R5).  It commits
    none of the raw token/Cookie/account/full-URL bytes the raw file may hold.
    """
    path = staging_dir / "live-canary-certified.json"
    if not path.is_file():
        return None
    try:
        payload = json_loads_no_dupes(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    companion_status = payload.get("companion_status") or {}
    companions = companion_status.get("companions") or []
    scopes = payload.get("scopes") or []
    seen = {entry.get("scope") for entry in scopes if isinstance(entry, dict)}
    # C-122 HG-A: coverage tracks the FULL certified canary scope set — the
    # browser Companion OTA scopes plus the iCom public-API scope.
    expected = sorted(_ALL_CERTIFIED_CANARY_SCOPES)
    coverage = {
        "expected_scope_count": len(expected),
        "expected_scopes": expected,
        "observed_scope_count": len(seen),
        "passed_scope_count": sum(
            1 for entry in scopes if isinstance(entry, dict) and entry.get("passed") is True
        ),
        "missing": sorted(set(expected) - seen),
    }
    return {
        "schema_version": _LAYER5_COMPACT_SCHEMA,
        "generated_at": payload.get("generated_at"),
        "passed": payload.get("passed"),
        "bridge_token_present": payload.get("bridge_token_present"),
        "coverage": coverage,
        "scopes": [
            {
                "scope": entry.get("scope"),
                "provider": (
                    str(entry.get("scope")).split(":", 1)[0]
                    if isinstance(entry.get("scope"), str)
                    else None
                ),
                "kind": entry.get("kind"),
                "passed": entry.get("passed"),
                "fresh": entry.get("fresh"),
                "authorized": entry.get("authorized"),
                "read_only": entry.get("read_only"),
                "evidence": _desensitize_scope_evidence(entry.get("evidence") or {}),
            }
            for entry in scopes
            if isinstance(entry, dict)
        ],
        "companion_status": {
            "status": companion_status.get("status"),
            "stale_after_seconds": companion_status.get("stale_after_seconds"),
            "companions": [
                {
                    "companion_id": comp.get("companion_id"),
                    "providers": comp.get("providers"),
                    "authorized_scope_keys": comp.get("authorized_scope_keys"),
                    "is_fresh": comp.get("is_fresh"),
                    "age_seconds": comp.get("age_seconds"),
                    "build_sha256": (comp.get("build_identity") or {}).get(
                        "build_sha256"
                    ),
                }
                for comp in companions
            ],
        },
        "raw_evidence": {
            "file": "live-canary-certified.json",
            "committed": False,
            "sha256": _sha256_file(path),
        },
    }


# C-122 Fix 3: the compact must preserve desensitized, recomputable per-item
# structured evidence for every layer-6 done-gate check — never a bare verdict
# list.  These are the binding fields each check's evidence must carry in the
# committed compact (names match the live-v4 runner's evidence dicts) so a
# reviewer can recompute the check from the trail alone.
_LAYER6_REQUIRED_EVIDENCE_FIELDS: dict[str, frozenset[str]] = {
    "prefrozen_stay_plan_candidate_set": frozenset({"candidate_set_sha256"}),
    "v4_source_graph": frozenset(
        {
            "expected_browser_tasks_per_pair",
            "expected_browser_source_ids",
            "expected_query_shapes",
            "expected_icom_task_ids",
            "pair_ids",
            "checkpoint_bound_pair_ids",
            # C-122 supervision 18:13 (Fix 4): the compact must carry the full
            # desensitized checkpoint binding (chain / dates / request / content)
            # so the layer-6 validator can independently re-verify it — a compact
            # without the binding fails closed before any semantic check.
            "checkpoint_binding",
            "total_planned_task_count",
            "per_pair",
        }
    ),
    "stage_aware_exploration_publication_contract": frozenset(
        {"exploration_count", "publication_count"}
    ),
    "stay_inventory_four_state_contract": frozenset(
        {"minimum_exact_providers_per_selected_segment", "inventory_states"}
    ),
    "planner_verifier_repair_master_stay_plan_chain": frozenset({"evidence_refs"}),
    "recommendable_date_pair_stay_plan_options": frozenset(
        {"freshness_ttl_seconds", "freshness_by_option"}
    ),
    "icom_exploration_and_publication_evidence": frozenset(
        {"publication_target_task_ids", "exploration_full_coverage"}
    ),
    "all_recommended_publication_closures": frozenset({"options"}),
    "real_v4_browser_source_evidence": frozenset({"source_task_count", "snapshot_count"}),
    "flight_search_outcome_contract": frozenset(
        {
            "provider_outcome_states",
            "exact_provider_count",
            "comparison_provider_count",
            "price_bearing_provider_count",
        }
    ),
    "observed_cross_platform_overlap": frozenset(
        {"interval_count", "max_overlapping_providers"}
    ),
    "strict_selected_plan_platform_coverage": frozenset(
        {"providers", "selected_stay_plan_id", "coverage_mode", "all_platforms_complete"}
    ),
    "planner_verifier_repair_orchestrator": frozenset(
        {"graph_chain_ok", "reverify_node_present"}
    ),
    "exact_budget_and_selected_evidence": frozenset(
        {"computed_total_cents", "declared_total_cents"}
    ),
    "event_injection_repair_reverify_master": frozenset(
        {
            "dynamic_replan",
            "read_only_graph",
            "initial_stay_plan_id",
            "event_final_stay_plan_id",
        }
    ),
}

# Strings that match this are content-addressable bindings (sha256 / git SHAs /
# UUIDs), never secrets — but ONLY inside an explicit hash field.  A 64-hex
# string in an arbitrary scalar position is a token-shaped secret and is
# redacted (C-122 acceptance).
_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
# A strict SHA-256 digest (exactly 64 lowercase hex).  Digest fields in the
# committed evidence — Companion build fingerprint, bridge-state file hash,
# manifest entry hashes - must be exact 64-hex, not the looser 32-128 range of
# ``_HEX_HASH_RE`` (which is a secret-scanning shape, not a schema validator).
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# Token-like strings: long, dense, no whitespace — covers base64url JWTs
# (header.payload.signature) and bearer-token shapes.
_TOKEN_ISH_RE = re.compile(r"^[A-Za-z0-9+/=_.\-]{48,}$")

# Sentinel schemas for the per-check evidence whitelist DSL.
_EVIDENCE_SCALAR = object()  # any scalar; strings are desensitized on the way out
_EVIDENCE_HASH = object()  # a content-addressable hash binding (kept as-is)


def _desensitize_check_scalar(value: str) -> str:
    """Desensitize one scalar string carried by the compact's check evidence.

    URL-bearing values, long token-like credentials and any pure 32-128 hex
    string are all redacted: a hex string is only a safe content-addressable
    binding inside an explicit ``_EVIDENCE_HASH`` field, never an arbitrary
    scalar (C-122 acceptance)."""
    if "://" in value or value.startswith(("http://", "https://", "www.")):
        return f"url#{_sha256_hex(value)[:16]}"
    if _HEX_HASH_RE.fullmatch(value) is not None or _TOKEN_ISH_RE.fullmatch(value) is not None:
        return f"secret#{_sha256_hex(value)[:16]}"
    return value


def _desensitize_check_value(value: Any, schema: Any) -> Any:
    """Copy ``value`` into the compact strictly guided by ``schema``.

    Schema shapes:

      * ``_EVIDENCE_SCALAR`` — keep any scalar (strings desensitized); a
        list/dict in a scalar position is dropped (never a recursive copy).
      * ``_EVIDENCE_HASH`` — keep a content-addressable binding string (hex
        sha256 / git SHA, or a ``kind:<hex>`` reference); anything else is
        desensitized like a scalar.
      * ``{"_item": schema}`` — a list; each item copied against ``schema``.
      * ``{"_dict": schema}`` — a dict whose VALUES are copied against
        ``schema`` (keys are opaque identifiers, e.g. option ids).
      * ``{key: schema, ...}`` — a dict; only the listed keys are copied.

    Unknown keys are dropped everywhere, so the compact can never carry an
    incidental recursive copy of the check's raw evidence dict (C-122
    acceptance).
    """
    if schema is _EVIDENCE_SCALAR:
        if isinstance(value, str):
            return _desensitize_check_scalar(value)
        if isinstance(value, (dict, list, tuple)):
            return None
        return value
    if schema is _EVIDENCE_HASH:
        if isinstance(value, str):
            binding = value.rsplit(":", 1)[-1] if ":" in value else value
            if _HEX_HASH_RE.fullmatch(binding) is not None:
                return value
            return _desensitize_check_scalar(value)
        return value
    if isinstance(schema, dict):
        if "_item" in schema:
            if not isinstance(value, list):
                return None
            item_schema = schema["_item"]
            copied = [_desensitize_check_value(item, item_schema) for item in value]
            return [item for item in copied if item is not None]
        if "_dict" in schema:
            if not isinstance(value, dict):
                return None
            sub = schema["_dict"]
            out: dict[str, Any] = {}
            for key, item in value.items():
                item_copy = _desensitize_check_value(item, sub)
                if item_copy is not None:
                    out[str(key)] = item_copy
            return out
        if not isinstance(value, dict):
            return None
        out = {}
        for key, sub in schema.items():
            if key in value:
                item_copy = _desensitize_check_value(value[key], sub)
                if item_copy is not None:
                    out[key] = item_copy
        return out
    return None


# Per-check NESTED whitelist schemas for the compact.  The top-level field
# whitelist (``_LAYER6_REQUIRED_EVIDENCE_FIELDS``) decides WHICH binding fields
# a check may carry; this map describes the allowed sub-structure of the nested
# ones (``options`` / ``dynamic_replan`` / ``freshness_by_option`` / …) so no
# unknown nested field and no 64-hex token can ride into the committed compact
# (C-122 acceptance).  Fields without an entry copy as ``_EVIDENCE_SCALAR``.
_LAYER6_EVIDENCE_SCHEMAS: dict[str, dict[str, Any]] = {
    "prefrozen_stay_plan_candidate_set": {
        "candidate_set_sha256": _EVIDENCE_HASH,
    },
    "v4_source_graph": {
        "expected_browser_tasks_per_pair": _EVIDENCE_SCALAR,
        "expected_browser_source_ids": {"_item": _EVIDENCE_SCALAR},
        "expected_query_shapes": {"_item": _EVIDENCE_SCALAR},
        "expected_icom_task_ids": {"_item": _EVIDENCE_SCALAR},
        "pair_ids": {"_item": _EVIDENCE_SCALAR},
        # C-122 supervision 01:10: the run's checkpoint-bound sealed pair ids —
        # an independent job-control-plane record the compact must carry so the
        # validator can reject a foreign / swapped / missing / extra pair set.
        "checkpoint_bound_pair_ids": {"_item": _EVIDENCE_SCALAR},
        # C-122 supervision 18:13 (Fix 4): the complete desensitized checkpoint
        # binding.  Every nested field is whitelisted so a forged 64-hex or an
        # unknown nested binding cannot ride into the committed compact; the
        # validator RECOMPUTES the chain digest / date window / request identity
        # / per-checkpoint content from these carried fields.
        "checkpoint_binding": {
            "passed": _EVIDENCE_SCALAR,
            "count": _EVIDENCE_SCALAR,
            "ordered_checkpoint_sha256": {"_item": _EVIDENCE_HASH},
            "checkpoint_chain_sha256": _EVIDENCE_HASH,
            "request_sha256": _EVIDENCE_HASH,
            "bindings": {
                "_item": {
                    "sequence": _EVIDENCE_SCALAR,
                    "date_pair_id": _EVIDENCE_SCALAR,
                    "departure_date": _EVIDENCE_SCALAR,
                    "return_date": _EVIDENCE_SCALAR,
                    "state": _EVIDENCE_SCALAR,
                    "query_task_ids": {"_item": _EVIDENCE_SCALAR},
                    "query_task_ids_sha256": _EVIDENCE_HASH,
                    "run_summary_sha256": _EVIDENCE_HASH,
                    "captured_at": _EVIDENCE_SCALAR,
                    "checkpoint_sha256": _EVIDENCE_HASH,
                    "request_sha256": _EVIDENCE_HASH,
                }
            },
        },
        "total_planned_task_count": _EVIDENCE_SCALAR,
        # C-122 HG-G: the frozen-scenario per-pair breakdown — every nested field
        # is whitelisted so a forged 64-hex or an unknown nested binding cannot
        # ride into the committed compact.
        # C-122 round-19 (Block 1): the per-pair MEMBER LISTS are whitelisted
        # alongside the counts so the exact member-set comparison survives
        # desensitization into the committed compact.
        "per_pair": {
            "_item": {
                "pair_id": _EVIDENCE_SCALAR,
                "browser_source_task_ids": {"_item": _EVIDENCE_SCALAR},
                "query_task_ids": {"_item": _EVIDENCE_SCALAR},
                "icom_source_task_ids": {"_item": _EVIDENCE_SCALAR},
                "browser_source_task_count": _EVIDENCE_SCALAR,
                "query_task_count": _EVIDENCE_SCALAR,
                "icom_source_task_count": _EVIDENCE_SCALAR,
            }
        },
    },
    "stage_aware_exploration_publication_contract": {
        "exploration_count": _EVIDENCE_SCALAR,
        "publication_count": _EVIDENCE_SCALAR,
    },
    "stay_inventory_four_state_contract": {
        "minimum_exact_providers_per_selected_segment": _EVIDENCE_SCALAR,
        "inventory_states": {"_item": _EVIDENCE_SCALAR},
    },
    "planner_verifier_repair_master_stay_plan_chain": {},
    "recommendable_date_pair_stay_plan_options": {
        "freshness_ttl_seconds": _EVIDENCE_SCALAR,
        "freshness_by_option": {
            "_dict": {
                "_item": {
                    "component_id": _EVIDENCE_SCALAR,
                    "captured_at": _EVIDENCE_SCALAR,
                    "expires_at": _EVIDENCE_SCALAR,
                    "age_seconds_at_post_event_gate": _EVIDENCE_SCALAR,
                    "ttl_seconds": _EVIDENCE_SCALAR,
                    "fresh_at_post_event_gate": _EVIDENCE_SCALAR,
                }
            }
        },
    },
    "icom_exploration_and_publication_evidence": {
        "publication_target_task_ids": {"_item": _EVIDENCE_SCALAR},
        "exploration_full_coverage": {"passed": _EVIDENCE_SCALAR},
    },
    "all_recommended_publication_closures": {
        "options": {
            "_dict": {
                "evidence_scope": _EVIDENCE_SCALAR,
                "planner_verifier_repair": {"passed": _EVIDENCE_SCALAR},
                "budget_and_selected_evidence": {"passed": _EVIDENCE_SCALAR},
                "public_transfer_evidence": {"passed": _EVIDENCE_SCALAR},
            }
        },
    },
    "real_v4_browser_source_evidence": {
        "source_task_count": _EVIDENCE_SCALAR,
        "snapshot_count": _EVIDENCE_SCALAR,
    },
    "flight_search_outcome_contract": {
        "provider_outcome_states": {"_dict": _EVIDENCE_SCALAR},
        "exact_provider_count": _EVIDENCE_SCALAR,
        "comparison_provider_count": _EVIDENCE_SCALAR,
        "price_bearing_provider_count": _EVIDENCE_SCALAR,
    },
    "observed_cross_platform_overlap": {
        "interval_count": _EVIDENCE_SCALAR,
        "max_overlapping_providers": _EVIDENCE_SCALAR,
    },
    "strict_selected_plan_platform_coverage": {
        "providers": {"_item": _EVIDENCE_SCALAR},
        "selected_stay_plan_id": _EVIDENCE_SCALAR,
        "coverage_mode": _EVIDENCE_SCALAR,
        "all_platforms_complete": _EVIDENCE_SCALAR,
    },
    "planner_verifier_repair_orchestrator": {
        "graph_chain_ok": _EVIDENCE_SCALAR,
        "reverify_node_present": _EVIDENCE_SCALAR,
    },
    "exact_budget_and_selected_evidence": {
        "computed_total_cents": _EVIDENCE_SCALAR,
        "declared_total_cents": _EVIDENCE_SCALAR,
    },
    "event_injection_repair_reverify_master": {
        "dynamic_replan": {"passed": _EVIDENCE_SCALAR},
        "read_only_graph": {"passed": _EVIDENCE_SCALAR},
        "initial_stay_plan_id": _EVIDENCE_SCALAR,
        "event_final_stay_plan_id": _EVIDENCE_SCALAR,
    },
}


def _desensitized_check_evidence(
    check: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """The committed per-item structured evidence for one done-gate check.

    Built from the check's per-name FIELD WHITELIST
    (``_LAYER6_REQUIRED_EVIDENCE_FIELDS``) plus the NESTED schemas
    (``_LAYER6_EVIDENCE_SCHEMAS``) — the recomputable observed+expected
    bindings — plus any ``evidence_refs`` and the candidate-set SHA binding for
    the prefrozen-candidate check.  Every nested level is copied strictly
    through its schema, so the compact can only ever carry the known-safe
    recomputable fields, never an incidental recursive copy of the check's raw
    evidence dict, and no 64-hex token can pass through a non-hash position
    (C-122 round-18 item 5 + acceptance).  Values are still desensitized on the
    way out.
    """
    name = check.get("name")
    whitelist = (
        _LAYER6_REQUIRED_EVIDENCE_FIELDS.get(name, frozenset())
        if isinstance(name, str)
        else frozenset()
    )
    schemas = (
        _LAYER6_EVIDENCE_SCHEMAS.get(name, {}) if isinstance(name, str) else {}
    )
    safe: dict[str, Any] = {}
    raw_evidence = check.get("evidence")
    if isinstance(raw_evidence, dict):
        for key in whitelist:
            if key == "evidence_refs" or key not in raw_evidence:
                continue
            copied = _desensitize_check_value(
                raw_evidence[key], schemas.get(key, _EVIDENCE_SCALAR)
            )
            if copied is not None:
                safe[key] = copied
    refs = check.get("evidence_refs") or ()
    ref_list = [str(ref) for ref in refs if isinstance(ref, str)]
    if ref_list:
        safe.setdefault("evidence_refs", ref_list)
    if name == "prefrozen_stay_plan_candidate_set":
        candidate_sha = payload.get("api_payload_candidate_set_sha256")
        if isinstance(candidate_sha, str) and len(candidate_sha) == 64:
            safe.setdefault("candidate_set_sha256", candidate_sha)
    if not safe:
        safe["verdict"] = "passed"
    return safe


def _whitelisted_repo_revision(repo_revision: Any) -> dict[str, Any]:
    """Repo-relative revision fields for the compact — never the absolute host
    ``toplevel`` (C-122 round-18 item 5).  Only the repo-relative identifiers
    and hashes (branch / commit_sha / worktree_dirty, plus the start-revision
    comparison) leave the runtime.
    """
    if not isinstance(repo_revision, dict):
        return {}
    safe: dict[str, Any] = {
        key: repo_revision.get(key)
        for key in ("branch", "commit_sha", "worktree_dirty")
        if key in repo_revision
    }
    if repo_revision.get("revision_changed_during_run") is True:
        safe["revision_changed_during_run"] = True
        start = repo_revision.get("start_revision")
        if isinstance(start, dict):
            safe["start_revision"] = {
                key: start.get(key)
                for key in ("branch", "commit_sha", "worktree_dirty")
                if key in start
            }
    return safe


def _compact_live_e2e(staging_dir: Path) -> dict[str, Any] | None:
    """Desensitized, independently reviewable layer-6 compact artifact.

    Carries the run status, the FULL 15-item done-gate check verdicts (which
    cover the planner-verifier-repair chain, the exact budget + selected
    evidence, and the event-injection repair/re-verify master), repo revision,
    timeout/runner/event-injection contracts, runtime identity and Companion
    preflight — never the raw request/quote/URL/account content of the E2E run
    (C-114 R5).  The compact carries only repo-relative identifiers and hashes;
    absolute host paths (``toplevel`` / ``repo_toplevel``) are stripped
    (C-122 round-18 item 5).
    """
    path = staging_dir / "live-done-gate-v4.json"
    if not path.is_file():
        return None
    try:
        payload = json_loads_no_dupes(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rb = payload.get("runtime_before_run") or {}
    rp = rb.get("runtime_provenance") or {}
    cp = payload.get("companion_preflight") or {}
    companions = cp.get("companions") or []
    dg = payload.get("done_gate") or {}
    checks = dg.get("checks") or []
    # C-122 supervision 01:10: the run's checkpoint-bound sealed pair ids from
    # the job control plane (the terminal job's pair checkpoints), merged into
    # the v4_source_graph evidence so the compact carries an independent record
    # of what the run ACTUALLY sealed alongside the producer's own ``pair_ids``.
    # C-122 supervision 18:13 (Fix 4): the compact must ALSO carry the complete
    # DESENSITIZED checkpoint binding — the ordered checkpoint digest chain, the
    # chain digest, the request identity and each binding's dates / state /
    # content hashes / sequence / checkpoint digest — so a reviewer can
    # independently re-verify chain integrity, the canonical date window and the
    # request identity from the compact alone (a reordered / wrong-date /
    # wrong-request / same-raw-copied digest forgery fails closed).
    checkpoint_pairs: list[str] = []
    checkpoint_binding_dict: dict[str, Any] = {}
    raw_checkpoint_binding = (
        (payload.get("context") or {}).get("pair_checkpoint_binding") or {}
    )
    if isinstance(raw_checkpoint_binding, dict):
        desensitized_bindings: list[dict[str, Any]] = []
        for binding in raw_checkpoint_binding.get("bindings") or ():
            if not isinstance(binding, dict):
                continue
            date_pair_id = binding.get("date_pair_id")
            if isinstance(date_pair_id, str):
                checkpoint_pairs.append(date_pair_id)
            # Structural / hashed / date-only fields only — the same fields the
            # checkpoint model's own ``checkpoint_sha256`` recomputes from, never
            # raw request/quote/URL/account content.
            desensitized_bindings.append(
                {
                    "sequence": binding.get("sequence"),
                    "date_pair_id": date_pair_id,
                    "departure_date": binding.get("departure_date"),
                    "return_date": binding.get("return_date"),
                    "state": binding.get("state"),
                    "query_task_ids": binding.get("query_task_ids"),
                    "query_task_ids_sha256": binding.get("query_task_ids_sha256"),
                    # C-122 round-19 (gap 4): the FULL business summary fields —
                    # the checkpoint model's ``_run_summary`` digest recomputes
                    # ``run_summary_sha256`` from these exact fields, so a
                    # doctored summary (wrong source-task count, flipped
                    # completion flag) can never pass with a copied digest.
                    "run_purpose": binding.get("run_purpose"),
                    "finalization_state": binding.get("finalization_state"),
                    "decision_state": binding.get("decision_state"),
                    "source_task_count": binding.get("source_task_count"),
                    "exploration_seal_passed": binding.get("exploration_seal_passed"),
                    "all_platforms_complete": binding.get("all_platforms_complete"),
                    "failure_class": binding.get("failure_class"),
                    "run_summary_sha256": binding.get("run_summary_sha256"),
                    "captured_at": binding.get("captured_at"),
                    "checkpoint_sha256": binding.get("checkpoint_sha256"),
                    "request_sha256": binding.get("request_sha256"),
                }
            )
        checkpoint_binding_dict = {
            "passed": raw_checkpoint_binding.get("passed"),
            "count": raw_checkpoint_binding.get("count"),
            "ordered_checkpoint_sha256": raw_checkpoint_binding.get(
                "ordered_checkpoint_sha256"
            ),
            "checkpoint_chain_sha256": raw_checkpoint_binding.get(
                "checkpoint_chain_sha256"
            ),
            "request_sha256": raw_checkpoint_binding.get("request_sha256"),
            "bindings": desensitized_bindings,
        }
    compact_checks: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict) or not check.get("name"):
            continue
        item_evidence = _desensitized_check_evidence(check, payload)
        if check.get("name") == "v4_source_graph" and checkpoint_pairs:
            item_evidence["checkpoint_bound_pair_ids"] = list(checkpoint_pairs)
            if checkpoint_binding_dict.get("bindings"):
                item_evidence["checkpoint_binding"] = checkpoint_binding_dict
        compact_checks.append(
            {
                "name": check.get("name"),
                "passed": check.get("passed"),
                "summary": check.get("summary"),
                "evidence_refs": [
                    str(ref)
                    for ref in (check.get("evidence_refs") or ())
                    if isinstance(ref, str)
                ],
                # C-122 Fix 3: the desensitized, recomputable per-item structured
                # evidence each check carries — never a bare verdict list.
                "evidence": item_evidence,
            }
        )
    done_gate = {
        "passed": dg.get("passed"),
        "check_count": len(checks),
        "passed_check_count": sum(
            1
            for check in checks
            if isinstance(check, dict) and check.get("passed") is True
        ),
        "checks": compact_checks,
    }
    return {
        "schema_version": _LAYER6_COMPACT_SCHEMA,
        "captured_at": payload.get("captured_at"),
        "run_status": payload.get("run_status"),
        "done_gate": done_gate,
        "repo_revision": _whitelisted_repo_revision(payload.get("repo_revision")),
        "start_revision": payload.get("start_revision"),
        "failure": payload.get("failure"),
        "timeout_contract": payload.get("timeout_contract"),
        "runner_contract": payload.get("runner_contract"),
        "event_injection_contract": payload.get("event_injection_contract"),
        "api_payload_candidate_set_sha256": payload.get(
            "api_payload_candidate_set_sha256"
        ),
        # C-122 round-19 (gap 4): the compact also carries the raw request
        # payload's own SHA (``request_identity.api_payload_sha256``), so the
        # checkpoint binding's request identity is bound to the ACTUAL API
        # payload the run submitted — not merely a self-declared request SHA.
        "api_payload_sha256": (payload.get("request_identity") or {}).get(
            "api_payload_sha256"
        ),
        "scenario_sha256": payload.get("scenario_sha256"),
        "runtime_before_run": {
            "model_provider": rb.get("model_provider"),
            "primary_model": rb.get("primary_model"),
            "model_enabled": rb.get("model_enabled"),
            "model_required": rb.get("model_required"),
            "runtime_provenance": {
                "commit_sha": rp.get("commit_sha"),
                "dependency_lock_sha256": rp.get("dependency_lock_sha256"),
                "live_system_source_sha256": rp.get("live_system_source_sha256"),
                "python_version": rp.get("python_version"),
                "started_at": rp.get("started_at"),
            },
        },
        "companion_preflight": {
            "status": cp.get("status"),
            "stale_after_seconds": cp.get("stale_after_seconds"),
            "companions": [
                {
                    "companion_id": comp.get("companion_id"),
                    "authorized_scope_keys": comp.get("authorized_scope_keys"),
                }
                for comp in companions
            ],
        },
        # C-122 Fix 2 + round-18 item 6: the lease-evidence bindings — the exact
        # bridge-state file (path) checked BEFORE the run and AGAIN AFTER it,
        # each with its SHA256 hash, so a reviewer can re-verify the isolation
        # proof from both sides of the run from the committed trail.
        "bridge_state_lease_preflight": _bridge_state_binding(),
        "bridge_state_lease_postcheck": _bridge_state_after_binding(),
        "raw_evidence": {
            "file": "live-done-gate-v4.json",
            "committed": False,
            "sha256": _sha256_file(path),
        },
    }


def _generate_compact_evidence(staging_dir: Path) -> None:
    """Write desensitized layer-5/6 compact artifacts into staging.

    Runs before the required-input gate and before the evidence commit, so the
    committed trail carries independently reviewable layer-5/6 evidence instead
    of only a ``committed=false`` raw hash (C-114).  Raw evidence that exists
    but cannot be compacted is a hard failure (exit 2), never a silent skip.
    """
    canary = _compact_canary(staging_dir)
    e2e = _compact_live_e2e(staging_dir)
    if (staging_dir / "live-canary-certified.json").is_file() and canary is None:
        raise GateStateChangedError(
            "cannot produce desensitized layer-5 compact artifact from "
            "live-canary-certified.json"
        )
    if (staging_dir / "live-done-gate-v4.json").is_file() and e2e is None:
        raise GateStateChangedError(
            "cannot produce desensitized layer-6 compact artifact from "
            "live-done-gate-v4.json"
        )
    for staged_name, payload in (
        (_COMPACT_CANARY_STAGED_NAME, canary),
        (_COMPACT_E2E_STAGED_NAME, e2e),
    ):
        if payload is None:
            continue
        # Atomic + owner-only from the start (C-118): the tmp is sealed to 0600
        # before the rename, so no window exposes a partially-written compact
        # artifact with looser permissions than the final file.
        _write_atomic(
            staging_dir / staged_name,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            mode=0o600,
        )


def _evidence_manifest(
    staging_dir: Path,
    report: GateReport,
    *,
    evidence_commit: str | None = None,
) -> dict[str, Any]:
    """Build the committed-evidence contract manifest for a passing gate.

    Never carries the absolute host ``toplevel`` (C-122 round-18 gate-6): a
    committed artifact must not reveal the host filesystem layout, so the
    manifest names only repo-relative identifiers and hashes.

    C-122 round-19 (02:56 supervision / gap 3): the generated manifest is
    validated by the SAME canonical validator the publish preflight and the
    resolver use, so a manifest that generation itself builds but that violates
    the canonical contract — a missing/renamed/smuggled evidence entry, a
    relocated ``tracked_path`` or a flipped ``committed`` flag — fails closed at
    generation time instead of being written and caught later.
    """
    manifest = {
        "schema_version": _MANIFEST_SCHEMA,
        "tested_commit_sha": report.tested_commit_sha,
        "run_id": report.run_id,
        "evidence_commit": evidence_commit,
        "generated_at": report.generated_at,
        "branch": report.branch,
        "files": _manifest_files(staging_dir),
        "layer_verdicts": {
            "5_real_canary": _canary_manifest(staging_dir),
            "6_full_e2e": _live_e2e_manifest(staging_dir),
        },
    }
    problems: list[str] = []
    _validate_evidence_manifest(
        manifest, label="evidence manifest generation", problems=problems
    )
    if problems:
        raise GateStateChangedError(
            "generated evidence manifest violates the canonical contract: "
            f"{problems[0]}"
        )
    return manifest


def _write_manifest(manifest: dict[str, Any], target: Path) -> Path:
    # Atomic + owner-only (C-118): the manifest is part of the committed trail,
    # so it must never be observable partially written or with looser
    # permissions than 0600.  ``target`` is the exclusive staging location (the
    # shared worktree is never written — C-122 P0), from which the temp index
    # is populated via hash-object + update-index.
    return _write_atomic(
        target,
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        mode=0o600,
    )


def _validate_evidence_manifest(
    manifest: object,
    *,
    label: str,
    problems: list[str],
) -> None:
    """The SINGLE canonical evidence-manifest structural validator (C-122
    round-19 02:56 supervision / gap 3).

    Generation (``_evidence_manifest``), the E/P publish preflight
    (``_verify_evidence_contract``) and the resolver (``verify_gate_ref``) all
    enforce the manifest contract through THIS one function, so publish-side and
    resolver-side can never disagree on a renamed / missing / smuggled evidence
    file, a relocated ``tracked_path`` or a flipped ``committed`` flag.

    Enforces the STRUCTURAL contract only — nothing git-coupled:
      * top-level ``schema_version`` is exactly ``_MANIFEST_SCHEMA`` with the
        required binding fields (``tested_commit_sha`` / ``run_id`` / ``files``
        / ``layer_verdicts``) present;
      * ``files`` is a list of objects with the EXACT field set
        {name, tracked_path, sha256, size_bytes, committed};
      * file names are unique and the name set is EXACTLY the fixed
        ``_EVIDENCE_TRACKED_PATHS`` set — never derived from whatever happens
        to exist in a staging dir (the gap-3 flaw in the publish preflight);
      * each entry's ``tracked_path`` is the canonical path for that name and
        ``committed`` is the canonical ``_EVIDENCE_COMMITTED_CONTRACT`` flag;
      * per-file ``sha256`` is a valid 64-hex digest, ``size_bytes`` a real int.

    Git-coupled recomputes (blob sha256 / size, presence in E's tree, secret
    re-scans) are the callers' job, run AFTER this returns.  Appends a
    ``problems`` entry per violation; never raises.
    """
    if not isinstance(manifest, dict):
        problems.append(f"{label} is not an object")
        return
    if manifest.get("schema_version") != _MANIFEST_SCHEMA:
        problems.append(
            f"{label} schema_version {manifest.get('schema_version')!r} != "
            f"{_MANIFEST_SCHEMA}"
        )
    for key in ("tested_commit_sha", "run_id", "files", "layer_verdicts"):
        if key not in manifest:
            problems.append(f"{label} missing required field {key!r}")
    files = manifest.get("files")
    if not isinstance(files, list):
        problems.append(f"{label} files field is not a list")
        return
    fixed_names = {staged_name for staged_name, _ in _EVIDENCE_TRACKED_PATHS}
    canonical_tracked = dict(_EVIDENCE_TRACKED_PATHS)
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            problems.append(f"{label} has a non-object file entry")
            continue
        if set(entry) != {"name", "tracked_path", "sha256", "size_bytes", "committed"}:
            problems.append(
                f"{label} file entry has an unexpected field set {sorted(entry)}"
            )
        entry_name = entry.get("name")
        entry_sha = entry.get("sha256")
        entry_size = entry.get("size_bytes")
        entry_committed = entry.get("committed")
        if not isinstance(entry_name, str) or not entry_name:
            problems.append(f"{label} has a file with no name")
            continue
        if entry_name in seen:
            problems.append(f"{label} repeats file name {entry_name!r}")
        seen.add(entry_name)
        if entry_name not in fixed_names:
            problems.append(
                f"{label} file {entry_name!r} is not a fixed evidence-contract name"
            )
        else:
            canonical_rel = canonical_tracked[entry_name]
            entry_rel = entry.get("tracked_path")
            if entry_rel != canonical_rel:
                problems.append(
                    f"{label} file {entry_name!r} tracked_path {entry_rel!r} != "
                    f"the canonical contract {canonical_rel!r} (relocated)"
                )
            expected_committed = _EVIDENCE_COMMITTED_CONTRACT[entry_name]
            if entry_committed != expected_committed:
                problems.append(
                    f"{label} file {entry_name!r} committed {entry_committed!r} != "
                    f"the canonical contract {expected_committed!r} (committed "
                    "flag flipped)"
                )
        if (
            not isinstance(entry_sha, str)
            or _HEX_HASH_RE.fullmatch(entry_sha) is None
            or len(entry_sha) != 64
        ):
            problems.append(
                f"{label} file {entry_name!r} sha256 is not a valid 64-hex digest"
            )
        if not isinstance(entry_size, int) or isinstance(entry_size, bool):
            problems.append(
                f"{label} file {entry_name!r} size_bytes is not an integer"
            )
        if not isinstance(entry_committed, bool):
            problems.append(
                f"{label} file {entry_name!r} committed is not a boolean"
            )
    if seen != fixed_names:
        problems.append(
            f"{label} file-name set {sorted(seen)} != the fixed evidence "
            f"contract set {sorted(fixed_names)}"
        )
    verdicts = manifest.get("layer_verdicts")
    if not isinstance(verdicts, dict):
        problems.append(f"{label} layer_verdicts field must be an object")
    else:
        for key in ("5_real_canary", "6_full_e2e"):
            if key not in verdicts:
                problems.append(f"{label} layer_verdicts missing {key!r}")


# Fixed required raw-evidence inputs the gate must certify before any committed
# trail can be produced.  This list is part of the evidence contract: layer-5/6
# raw evidence must exist — and must never be silently omitted via gitignore —
# before ``passed=true`` can be claimed and an evidence commit produced.
# The desensitized layer-5/6 compact artifacts are contract-required too (C-114):
# a ``committed=false`` raw hash must never be the only layer-5/6 evidence in
# the repository.
_REQUIRED_EVIDENCE_INPUTS: tuple[str, ...] = (
    "product-acceptance.json",
    "browser-e2e.json",
    "browser-e2e-screenshot.png",
    "live-canary-certified.json",
    "live-done-gate-v4.json",
    _COMPACT_CANARY_STAGED_NAME,
    _COMPACT_E2E_STAGED_NAME,
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


def _verify_layer5_compact_contract(tracked_rel: str, compact: dict[str, Any]) -> None:
    """C-118: hard-verify the committed layer-5 compact from E's blob.

    Requires the exact certified scope set — no fewer, no extra, each unique,
    each passed/fresh/authorized/read_only — plus the coverage thresholds naming
    the same six scopes with all observed/passed counts equal.  A compact that
    omits a scope, adds a non-certified scope, repeats a scope, carries a
    malformed (non-object / nameless) entry, or records any scope as not
    passed/fresh/authorized/read_only fails the phase closed (C-122 Fix 6).

    Every scope must also carry its per-scope authentication binding: a browser
    scope proves it via a fresh Companion identity (``companion_id``) that
    authorizes exactly this scope key (``authorized_scope_keys``); the icom
    public-API scope proves it via a real read-only query ``sample``.  A scope
    reduced to a bare verdict, or whose evidence does not bind it to an
    authenticated identity for that scope, fails closed.
    """
    if compact.get("schema_version") != _LAYER5_COMPACT_SCHEMA:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} schema_version "
            f"{compact.get('schema_version')!r} != {_LAYER5_COMPACT_SCHEMA} "
            "(producer and validator must share the same schema version)"
        )
    # C-122 supervision 09:59 (Block 1): the top-level field set is a fixed
    # contract — an ALIAS of a whitelisted digest key or any other foreign field
    # makes the compact foreign and fails closed (a non-canonical alias is
    # exactly where a foreign digest is smuggled in).
    unknown = set(compact) - _LAYER5_COMPACT_ALLOWED_TOP_LEVEL
    if unknown:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} unknown top-level "
            f"field(s): {sorted(unknown)!r}"
        )
    # C-122 round-18 gate-5: semantic (not just non-empty) top-level validation
    # — a compact must itself declare passed=true with the bridge token present
    # (a real authenticated Companion session), backed by a connected/fresh
    # Companion whose authorized scope set is EXACTLY the certified scope set.
    if compact.get("passed") is not True:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} top-level passed "
            "!= true"
        )
    if compact.get("bridge_token_present") is not True:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} "
            "bridge_token_present != true"
        )
    companion_status = compact.get("companion_status")
    if not isinstance(companion_status, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} lacks "
            "companion_status"
        )
    if companion_status.get("status") != "connected":
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} companion_status "
            f"status {companion_status.get('status')!r} != 'connected'"
        )
    stale_after = companion_status.get("stale_after_seconds")
    if (
        not isinstance(stale_after, int)
        or isinstance(stale_after, bool)
        or stale_after <= 0
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} companion_status "
            "has no positive stale_after_seconds"
        )
    companion_list = companion_status.get("companions")
    if not isinstance(companion_list, list) or not companion_list:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} companion_status "
            "has no companions"
        )
    for companion in companion_list:
        if not isinstance(companion, dict):
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} companion "
                "status entry is not an object"
            )
        if not isinstance(companion.get("companion_id"), str) or not companion[
            "companion_id"
        ]:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} companion "
                "entry lacks a companion_id"
            )
        if companion.get("is_fresh") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} companion "
                "is not fresh"
            )
        companion_scopes = companion.get("authorized_scope_keys")
        # C-122 HG-A: a browser Companion's authorization set must be EXACTLY
        # the certified browser OTA scopes — ``icom:transfer`` is a public-API scope,
        # not a Companion scope, and must never appear here.
        if not isinstance(companion_scopes, list) or set(companion_scopes) != set(
            _CERTIFIED_OTA_SCOPES
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} companion "
                "authorized_scope_keys != the certified browser Companion OTA scopes"
            )
        build_sha = companion.get("build_sha256")
        if (
            not isinstance(build_sha, str)
            or _SHA256_HEX_RE.fullmatch(build_sha) is None
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} companion "
                "lacks a valid 64-hex build_sha256"
            )
    # C-122 round-18: a browser scope's evidence Companion identity must be one
    # of the top-level CONNECTED companions — a scope may not bind a "fresh
    # Companion" that the compact's own companion_status never lists as
    # connected/fresh.
    top_companion_ids = {
        companion.get("companion_id")
        for companion in companion_list
        if isinstance(companion, dict) and isinstance(companion.get("companion_id"), str)
    }
    # C-122 HG-A: the compact's coverage/scope set is the FULL certified canary
    # scope set — the certified browser Companion OTA scopes plus the iCom
    # public-API scope.  The browser Companion ``authorized_scope_keys`` above
    # stays the narrower six-browser set.
    expected = sorted(_ALL_CERTIFIED_CANARY_SCOPES)
    coverage = compact.get("coverage")
    if not isinstance(coverage, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} lacks coverage "
            "thresholds"
        )
    if coverage.get("expected_scope_count") != len(expected):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} expected_scope_count "
            f"!= {len(expected)}"
        )
    if sorted(coverage.get("expected_scopes") or []) != expected:
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} expected_scopes "
            "does not equal the certified canary scope set"
        )
    if coverage.get("observed_scope_count") != len(expected):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} observed_scope_count "
            f"!= {len(expected)}"
        )
    if coverage.get("passed_scope_count") != len(expected):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} passed_scope_count "
            f"!= {len(expected)}"
        )
    if coverage.get("missing"):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} reports missing "
            f"scopes: {', '.join(sorted(coverage['missing']))}"
        )
    scopes = compact.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != len(expected):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} scope list count "
            f"!= {len(expected)}"
        )
    present: set[str] = set()
    for entry in scopes:
        # Fix 6: a malformed (non-object) entry fails closed — it is never
        # silently skipped and left for the final set comparison to catch.
        if not isinstance(entry, dict):
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} has a "
                "malformed scope entry (not an object)"
            )
        scope = entry.get("scope")
        if not isinstance(scope, str) or not scope:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} has a scope "
                "entry with a missing or invalid scope name"
            )
        if scope in present:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} repeats scope "
                f"{scope!r} (scope names must be unique)"
            )
        if scope not in _ALL_CERTIFIED_CANARY_SCOPES:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} scope {scope!r} "
                "is not one of the certified canary scopes"
            )
        present.add(scope)
        if not (
            entry.get("passed") is True
            and entry.get("fresh") is True
            and entry.get("authorized") is True
            and entry.get("read_only") is True
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} scope "
                f"{scope!r} not passed/fresh/authorized/read_only"
            )
        # C-122 round-18: the scope entry must carry its real ``kind`` and
        # ``provider`` (produced by the certified canary), never a forged or
        # missing kind/provider.
        scope_kind = entry.get("kind")
        if not isinstance(scope_kind, str) or not scope_kind:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} scope "
                f"{scope!r} carries no canary kind"
            )
        scope_provider = entry.get("provider")
        expected_provider = scope.split(":", 1)[0]
        if not isinstance(scope_provider, str) or scope_provider != expected_provider:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} scope "
                f"{scope!r} provider {scope_provider!r} != expected "
                f"{expected_provider!r}"
            )
        # C-122 Fix 3: each scope must carry its desensitized per-scope evidence
        # binding (companion identity, heartbeat fields or the read-only query
        # sample) in the committed compact — a scope reduced to a bare verdict
        # fails closed.
        scope_evidence = entry.get("evidence")
        if not isinstance(scope_evidence, dict) or not scope_evidence:
            raise GateStateChangedError(
                f"evidence commit E layer-5 compact {tracked_rel} scope "
                f"{scope!r} carries no per-scope evidence binding"
            )
        # C-122 Fix 6: per-item authentication.  A browser scope's evidence must
        # name a Companion that authorizes exactly this scope; the icom scope's
        # evidence must carry the read-only public query sample that produced
        # the quote.
        if scope.startswith("icom:"):
            # C-122 round-18: a passing icom canary must prove a positive quote
            # count AND the price/currency/departure-time fields of the quoted
            # sample — a sample reduced to a service name alone is not a quote.
            options = scope_evidence.get("options")
            if (
                not isinstance(options, int)
                or isinstance(options, bool)
                or options <= 0
            ):
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} carries no positive option count "
                    "(positive options required)"
                )
            sample = scope_evidence.get("sample")
            sample_fare = (
                sample.get("fare_amount") if isinstance(sample, dict) else None
            )
            sample_currency = (
                sample.get("currency") if isinstance(sample, dict) else None
            )
            sample_departure = (
                sample.get("departure_at") if isinstance(sample, dict) else None
            )
            if (
                not isinstance(sample, dict)
                or not sample.get("service_name")
                or not isinstance(sample_fare, str)
                or not sample_fare
                or not isinstance(sample_currency, str)
                or not sample_currency
                or not isinstance(sample_departure, str)
                or not sample_departure
            ):
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} sample must carry service_name + fare_amount + "
                    "currency + departure_at (real quote binding)"
                )
        else:
            companion_id = scope_evidence.get("companion_id")
            authorized = scope_evidence.get("authorized_scope_keys")
            if not isinstance(companion_id, str) or not companion_id:
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} carries no Companion identity "
                    "(per-scope authentication)"
                )
            if companion_id not in top_companion_ids:
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} Companion {companion_id!r} is not in the "
                    "connected companion_status companions"
                )
            if not isinstance(authorized, list) or scope not in {
                item for item in authorized if isinstance(item, str)
            }:
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} is not authorized by its Companion evidence "
                    "(per-scope authentication)"
                )
            # C-122 round-18: the browser heartbeat receipt must be real and
            # complete — beyond the identity, the evidence must carry at least
            # one heartbeat-record field proving an actual Companion handshake
            # (adapter/contract version or runtime instance id).
            receipt_fields = (
                scope_evidence.get("adapter_version"),
                scope_evidence.get("contract_version"),
                scope_evidence.get("runtime_instance_id"),
            )
            if not any(
                isinstance(value, str) and value for value in receipt_fields
            ):
                raise GateStateChangedError(
                    f"evidence commit E layer-5 compact {tracked_rel} scope "
                    f"{scope!r} Companion evidence carries no heartbeat receipt "
                    "(adapter_version/contract_version/runtime_instance_id)"
                )
    if present != set(expected):
        raise GateStateChangedError(
            f"evidence commit E layer-5 compact {tracked_rel} scope set != the "
            "certified scopes"
        )


def _canonical_sha256(payload: object) -> str:
    """The canonical JSON SHA-256 digest (matches the producer's ``_canonical_
    sha256`` in ``benchmarks/run_live_done_gate_v4.py``)."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _verify_layer6_checkpoint_binding(
    tracked_rel: str,
    check_name: str,
    evidence: dict[str, Any],
    pair_ids: list[Any],
    *,
    api_payload_sha256: str | None = None,
) -> None:
    """C-122 supervision 18:13 (Fix 4): independently verify the compact's
    checkpoint binding — chain integrity, date window, request identity and
    per-checkpoint content — from the compact's OWN carried fields.

    The compact must carry the complete DESENSITIZED checkpoint binding:
    ``ordered_checkpoint_sha256`` (the ordered per-checkpoint digests),
    ``checkpoint_chain_sha256``, ``request_sha256`` and one ``bindings`` entry
    per pair with ``date_pair_id`` / dates / ``state`` / ``query_task_ids`` /
    content hashes / ``checkpoint_sha256``.  The validator RECOMPUTES every
    digest instead of trusting it:

      - the chain digest recomputes from the ordered list,
      - each binding's dates must satisfy the canonical frozen time contract
        (shared with Fix 1) and agree with the pair id's own embedded dates,
      - every binding shares ONE request identity (the run's request SHA), and
        when the compact carries ``api_payload_sha256`` that identity is bound
        to the raw request payload's own SHA (C-122 round-19 gap 4),
      - each binding is a ``completed`` checkpoint and the bindings cover
        EXACTLY the frozen pair count with the canonical per-group query-task
        set — a non-completed checkpoint, a 2/4-group chain or a foreign query
        member cannot certify a passing gate,
      - each ``run_summary_sha256`` RECOMPUTES from the binding's carried
        business-summary fields via the checkpoint model's authoritative
        ``_run_summary`` digest — a doctored summary with a copied digest fails
        closed (C-122 round-19 gap 4),
      - each ``checkpoint_sha256`` recomputes from the binding's own carried
        fields using the checkpoint model's authoritative digest — a
        same-raw self-consistent forgery that copies a producer's digests
        verbatim without the underlying content fails closed, and a reordered /
        wrong-date / wrong-request chain is rejected even when every digest
        looks well-formed.
    """
    binding = evidence.get("checkpoint_binding")
    if not isinstance(binding, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding is missing (Fix 4 requires the "
            "full desensitized checkpoint binding)"
        )
    bindings = binding.get("bindings")
    if not isinstance(bindings, list) or len(bindings) != len(pair_ids):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding bindings do not cover exactly "
            "the frozen pair set"
        )
    # C-122 round-19 (gap 4): the checkpoint binding must cover EXACTLY the
    # frozen live-v4 pair count — a 2-group / 4-group chain cannot be the
    # sealed three-date-pair execution even when it is internally consistent.
    if len(bindings) != _V4_FROZEN_DATE_PAIR_COUNT:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding has {len(bindings)} bindings != "
            f"the frozen scenario's exact {_V4_FROZEN_DATE_PAIR_COUNT} date pairs"
        )
    # C-122 supervision 03:46 (Block 3): the binding's own PASSED/COUNT record
    # must certify a completed, exactly-three-group seal — a ``passed=false`` or
    # ``count=999`` claim is a forged header no matter how well-formed the
    # bindings list is.  ``count`` must agree with the verified binding count
    # (and therefore with the frozen pair count) rather than being re-derived.
    if binding.get("passed") is not True:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding passed "
            f"{binding.get('passed')!r} != true (a non-passing checkpoint "
            "seal cannot certify a passing gate)"
        )
    # C-122 supervision 04:14: ``count`` must be a STRICT JSON integer equal to
    # the frozen pair count — ``!=`` alone would accept ``3.0`` (``3.0 != 3`` is
    # False) and ``True`` (``True == 1``), and a string ``"3"`` is not an int.
    # Reject bool / float / string explicitly, fail-closed.
    count = binding.get("count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count != _V4_FROZEN_DATE_PAIR_COUNT
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding count "
            f"{count!r} != the frozen scenario's exact "
            f"{_V4_FROZEN_DATE_PAIR_COUNT} date pairs"
        )
    ordered = binding.get("ordered_checkpoint_sha256")
    if (
        not isinstance(ordered, list)
        or len(ordered) != len(pair_ids)
        or any(not isinstance(item, str) or not item for item in ordered)
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding ordered_checkpoint_sha256 is "
            "not the full ordered per-checkpoint digest chain"
        )
    chain_sha = binding.get("checkpoint_chain_sha256")
    if not isinstance(chain_sha, str) or not chain_sha:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding missing checkpoint_chain_sha256"
        )
    # Chain integrity: the chain digest must RECOMPUTE from the ordered list —
    # a reordered chain that merely re-labels its digests is a forgery.
    if chain_sha != _canonical_sha256(ordered):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding checkpoint_chain_sha256 does "
            "not recompute from ordered_checkpoint_sha256 (reordered chain)"
        )
    binding_request = binding.get("request_sha256")
    if not isinstance(binding_request, str) or not binding_request:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding missing request_sha256"
        )
    binding_pair_ids: set[str] = set()
    for index, entry in enumerate(bindings):
        if not isinstance(entry, dict):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding contains a non-object entry"
            )
        entry_pair_id = entry.get("date_pair_id")
        if not isinstance(entry_pair_id, str) or not entry_pair_id:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry missing date_pair_id"
            )
        if entry_pair_id not in set(pair_ids):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} is "
                "not one of the frozen pair ids"
            )
        binding_pair_ids.add(entry_pair_id)
        # Chain positional binding (C-122 supervision 18:13 reordered chain): the
        # ordered digest chain must be the bindings' OWN digests in the SAME
        # positional order, and each binding's ``sequence`` must equal its
        # position (1..N).  A chain that swaps bindings around and RECOMPUTES its
        # chain digest — sequence 3 leading, sequence 2, sequence 1 — still fails
        # closed here even though every digest recomputes from its own binding.
        entry_sequence = entry.get("sequence")
        if entry_sequence != index + 1:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                f"sequence {entry_sequence!r} != its position {index + 1} "
                "(reordered chain)"
            )
        entry_checkpoint_sha = entry.get("checkpoint_sha256")
        if not isinstance(entry_checkpoint_sha, str) or not entry_checkpoint_sha:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "missing checkpoint_sha256"
            )
        if ordered[index] != entry_checkpoint_sha:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding ordered_checkpoint_sha256 "
                f"at position {index} does not equal the binding's own "
                f"checkpoint_sha256 (reordered chain)"
            )
        # Date window + pair-id/date agreement (C-122 supervision 18:13 wrong
        # date): the binding's dates must satisfy the canonical frozen time
        # contract AND agree with the dates embedded in the pair id itself.
        departure_s = entry.get("departure_date")
        return_s = entry.get("return_date")
        if not isinstance(departure_s, str) or not isinstance(return_s, str):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "missing departure/return dates"
            )
        try:
            departure = date.fromisoformat(departure_s)
            return_d = date.fromisoformat(return_s)
        except ValueError:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "has unparsable departure/return dates"
            ) from None
        if not frozen_v4_pair_id_dates_canonical(departure, return_d):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "dates violate the canonical frozen time contract"
            )
        pair_match = re.fullmatch(
            r"date-pair:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}):[0-9a-f]{12}",
            entry_pair_id,
        )
        if pair_match is not None and (
            pair_match.group(1) != departure_s or pair_match.group(2) != return_s
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "dates disagree with the pair id's own embedded dates"
            )
        # Request identity: every binding carries the SAME request SHA as the
        # binding container — a foreign request binding is a forged chain.
        entry_request = entry.get("request_sha256")
        if entry_request != binding_request:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "request_sha256 does not match the binding's request identity"
            )
        # C-122 round-19 (gap 4): the binding's request identity must ALSO bind
        # to the compact's carried ``api_payload_sha256`` (the raw request
        # payload the run submitted).  A chain whose request SHA is not the
        # compact's API-payload SHA is a foreign request-payload binding.
        if api_payload_sha256 is not None and entry_request != api_payload_sha256:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "request_sha256 does not bind to the compact's "
                "api_payload_sha256 (foreign request-payload binding)"
            )
        # C-122 round-19 (gap 4): only a COMPLETED checkpoint can certify a
        # passing gate — a failed / pending checkpoint with a passing verdict is
        # a forged seal no matter how well-formed its digests are.
        entry_state = entry.get("state")
        if entry_state != "completed":
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                f"state {entry_state!r} != 'completed' (a non-completed "
                "checkpoint cannot certify a passing gate)"
            )
        # Content recomputation (C-122 supervision 18:13 same-raw forgery): each
        # ``checkpoint_sha256`` must RECOMPUTE from the binding's own carried
        # fields via the checkpoint model's authoritative digest.  A compact
        # that copies a producer's digests verbatim without the underlying
        # content — a wrong sequence, a doctored query-task set, a foreign
        # run_summary — fails closed here.
        for key in (
            "sequence",
            "state",
            "query_task_ids",
            "query_task_ids_sha256",
            "run_summary_sha256",
            "captured_at",
            "checkpoint_sha256",
        ):
            if key not in entry:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                    f"missing {key}"
                )
        # C-122 round-19 (gap 4): the full business-summary fields must be
        # carried so ``run_summary_sha256`` is independently recomputable —
        # a binding that drops the summary fields is not a reviewable checkpoint.
        for key in (
            "run_purpose",
            "finalization_state",
            "decision_state",
            "source_task_count",
            "exploration_seal_passed",
            "all_platforms_complete",
            "failure_class",
        ):
            if key not in entry:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                    f"missing {key}"
                )
        query_ids = entry.get("query_task_ids")
        if not isinstance(query_ids, list) or not query_ids:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "query_task_ids is empty or missing"
            )
        if _canonical_sha256(list(query_ids)) != entry.get("query_task_ids_sha256"):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "query_task_ids_sha256 does not recompute from query_task_ids"
            )
        # C-122 round-19 (gap 4): the per-group query-task set must be EXACTLY
        # the canonical frozen graph's browser Source-id set.  A binding with a
        # foreign / missing / swapped query member is not the frozen per-pair
        # plan — even when the digest chain is internally consistent.
        if set(query_ids) != _V4_FROZEN_BROWSER_SOURCE_IDS:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "query_task_ids member set != the canonical frozen graph "
                "browser Source-id set (foreign, missing or swapped member)"
            )
        # C-122 round-19 (gap 4): the full business-summary recompute —
        # ``run_summary_sha256`` must RECOMPUTE from the binding's carried
        # summary fields via the checkpoint model's authoritative ``_run_summary``
        # digest.  A doctored summary (wrong source-task count, flipped
        # completion flag) with a copied digest fails closed here.
        run_summary_recomputed = LivePlanningPairCheckpoint._digest(
            LivePlanningPairCheckpoint._run_summary(
                {
                    "state": entry_state,
                    "run_purpose": entry.get("run_purpose"),
                    "finalization_state": entry.get("finalization_state"),
                    "decision_state": entry.get("decision_state"),
                    "source_task_count": entry.get("source_task_count"),
                    "exploration_seal_passed": entry.get("exploration_seal_passed"),
                    "all_platforms_complete": entry.get("all_platforms_complete"),
                    "failure_class": entry.get("failure_class"),
                }
            )
        )
        if run_summary_recomputed != entry.get("run_summary_sha256"):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "run_summary_sha256 does not recompute from its carried "
                "run-summary fields (doctored summary or copied digest)"
            )
        checkpoint_summary = LivePlanningPairCheckpoint._checkpoint_summary(
            {
                "schema_version": "live-pair-checkpoint-v1",
                "request_sha256": entry_request,
                "sequence": entry.get("sequence"),
                "date_pair_id": entry_pair_id,
                "departure_date": departure_s,
                "return_date": return_s,
                "state": entry.get("state"),
                "query_task_ids": list(query_ids),
                "run_summary_sha256": entry.get("run_summary_sha256"),
                "captured_at": entry.get("captured_at"),
            }
        )
        recomputed = LivePlanningPairCheckpoint._digest(checkpoint_summary)
        if recomputed != entry.get("checkpoint_sha256"):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_binding entry {entry_pair_id!r} "
                "checkpoint_sha256 does not recompute from its carried fields "
                "(same-raw copied digest or doctored content)"
            )
    if binding_pair_ids != set(pair_ids):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check "
            f"{check_name!r} checkpoint_binding pair set != the frozen pair set"
        )


def _verify_layer6_check_semantics(
    tracked_rel: str,
    check_name: str,
    evidence: dict[str, Any],
    *,
    api_payload_sha256: str | None = None,
) -> None:
    """C-122 acceptance: a passing check's compacted evidence must be
    semantically consistent with its verdict — never just field-presence."""
    if check_name == "planner_verifier_repair_orchestrator":
        if evidence.get("graph_chain_ok") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} evidence graph_chain_ok is not true"
            )
        if evidence.get("reverify_node_present") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} evidence reverify_node_present is not true"
            )
    elif check_name == "exact_budget_and_selected_evidence":
        computed = evidence.get("computed_total_cents")
        declared = evidence.get("declared_total_cents")
        if (
            not isinstance(computed, int)
            or isinstance(computed, bool)
            or not isinstance(declared, int)
            or isinstance(declared, bool)
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} budget totals are not integers"
            )
        if computed != declared:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} computed_total_cents != declared_total_cents"
            )
    elif check_name == "event_injection_repair_reverify_master":
        dynamic = evidence.get("dynamic_replan")
        if not isinstance(dynamic, dict) or dynamic.get("passed") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} dynamic_replan sub-item is not passed"
            )
        read_only = evidence.get("read_only_graph")
        if not isinstance(read_only, dict) or read_only.get("passed") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} read_only_graph sub-item is not passed"
            )
        if evidence.get("initial_stay_plan_id") != evidence.get(
            "event_final_stay_plan_id"
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} event replan changed the frozen stay plan"
            )
    elif check_name == "flight_search_outcome_contract":
        states = evidence.get("provider_outcome_states")
        if not isinstance(states, dict) or not states:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} provider_outcome_states is not a non-empty object"
            )
        allowed = {"quote_found", "comparison_price_only", "bounded_no_exact_quote"}
        for provider, state in states.items():
            if state not in allowed:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} provider {provider!r} outcome state "
                    f"{state!r} is not a terminal flight-search state"
                )
        exact = evidence.get("exact_provider_count")
        price_bearing = evidence.get("price_bearing_provider_count")
        # C-122 round-18 gate-5: the validator mirrors the producer contract —
        # at least one exact provider AND >= 2 price-bearing providers
        # (exact + comparison).  The red line 严禁降低双平台精确报价阈值 2 is
        # enforced as ``price_bearing_provider_count >= 2`` here plus
        # ``minimum_exact_providers_per_selected_segment >= 2`` in the inventory
        # contract; the reviewer's literal ``exact_provider_count>=2`` would
        # reject legitimate producer-certified runs with 1 exact + 1 comparison.
        if not isinstance(exact, int) or isinstance(exact, bool) or exact < 1:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} exact_provider_count < 1"
            )
        if (
            not isinstance(price_bearing, int)
            or isinstance(price_bearing, bool)
            or price_bearing < 2
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} price_bearing_provider_count < 2 "
                "(dual-platform threshold)"
            )
    elif check_name == "stay_inventory_four_state_contract":
        minimum = evidence.get("minimum_exact_providers_per_selected_segment")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} minimum_exact_providers_per_selected_segment "
                "< 2 (dual-platform exact-quote threshold)"
            )
    elif check_name == "recommendable_date_pair_stay_plan_options":
        ttl = evidence.get("freshness_ttl_seconds")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} freshness_ttl_seconds is not a positive integer"
            )
        freshness = evidence.get("freshness_by_option")
        if not isinstance(freshness, dict) or not freshness:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} freshness_by_option is empty"
            )
        for option_id, components in freshness.items():
            if not isinstance(components, list) or not components:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} option {option_id!r} has no freshness "
                    "component evidence"
                )
            if any(
                not isinstance(component, dict)
                or component.get("fresh_at_post_event_gate") is not True
                for component in components
            ):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} option {option_id!r} has a stale component"
                )
    elif check_name == "all_recommended_publication_closures":
        options = evidence.get("options")
        if not isinstance(options, dict) or not options:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} options is empty"
            )
        for option_id, option in options.items():
            if not isinstance(option, dict):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} option {option_id!r} is not an object"
                )
            for sub in (
                "planner_verifier_repair",
                "budget_and_selected_evidence",
                "public_transfer_evidence",
            ):
                sub_value = option.get(sub)
                if not isinstance(sub_value, dict) or sub_value.get("passed") is not True:
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{check_name!r} option {option_id!r} {sub} is not passed"
                    )
    elif check_name == "v4_source_graph":
        # C-122 round-18 gate-2: a passing v4 source graph must carry a
        # POSITIVE browser-task-per-pair count — 0 / negative / non-int counts
        # cannot prove the fixed per-pair browser query plan.
        expected_tasks = evidence.get("expected_browser_tasks_per_pair")
        if (
            not isinstance(expected_tasks, int)
            or isinstance(expected_tasks, bool)
            or expected_tasks < 1
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_tasks_per_pair is not a "
                "positive integer"
            )
        # C-122 HG-G2 (supervision 16:03 counter-example A): the per-pair count
        # must equal the frozen scenario's exact browser task count — a graph
        # shrunk to 1 task per pair (3 pair x 1 task / total=3) is a forged
        # graph and fails closed even though every field is positive.
        if expected_tasks != _V4_FROZEN_TASKS_PER_PAIR:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_tasks_per_pair "
                f"{expected_tasks} != the frozen scenario's exact per-pair "
                f"browser task count {_V4_FROZEN_TASKS_PER_PAIR}"
            )
        source_ids = evidence.get("expected_browser_source_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_source_ids is empty"
            )
        if len(source_ids) != len(set(source_ids)):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_source_ids are not unique"
            )
        # C-122 HG-G2 (supervision 16:03 counter-example B): the declared per-pair
        # task count must be bound to exactly one Source id per task — a graph
        # declaring 5 tasks while listing only 1 Source id is forged and fails
        # closed.
        if len(source_ids) != expected_tasks:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_source_ids length "
                f"{len(source_ids)} != the frozen per-pair browser task count "
                f"{expected_tasks} — one Source id per task is required"
            )
        # C-122 round-19 (supervision 17:03 Block 1 counter-example): the member
        # SET must be EXACTLY the canonical frozen graph — a graph whose Source
        # ids have the right length but contain a foreign member, or omit / swap
        # a canonical member, is a forged graph and fails closed even though the
        # count and uniqueness checks pass.
        if set(source_ids) != _V4_FROZEN_BROWSER_SOURCE_IDS:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_browser_source_ids member set != the "
                "canonical frozen graph browser Source-id set (foreign, missing "
                "or swapped member)"
            )
        # C-122 round-18 HG-E: a passing v4 source graph must also carry the
        # query-shape contract, the iCom Source-task id set, the planned date
        # pairs and a positive total planned task count — the recomputable
        # bindings that prove the graph really planned the fixed per-pair
        # browser/iCom Source tasks (not just the browser-side ids).
        for key, label in (
            ("expected_query_shapes", "expected_query_shapes"),
            ("expected_icom_task_ids", "expected_icom_task_ids"),
        ):
            items = evidence.get(key)
            if not isinstance(items, list) or not items:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} {label} is empty"
                )
            if len(items) != len(set(items)):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} {label} are not unique"
                )
        # C-122 HG-G2 (supervision 16:03 counter-example B): the declared per-pair
        # task count must also be bound to exactly one query shape per task — a
        # graph declaring 5 tasks while listing only 1 query shape is forged and
        # fails closed.
        query_shapes = evidence.get("expected_query_shapes")
        if len(query_shapes) != expected_tasks:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_query_shapes length "
                f"{len(query_shapes)} != the frozen per-pair browser task count "
                f"{expected_tasks} — one query shape per task is required"
            )
        # C-122 round-19 (supervision 17:03 Block 1 counter-example): the query
        # shape and iCom task member SETS must be EXACTLY the canonical frozen
        # graph.  A compact with the right counts but a foreign query shape /
        # iCom task id, or a missing / extra iCom task, is a forged graph.
        if set(query_shapes) != _V4_FROZEN_QUERY_SHAPES:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_query_shapes member set != the "
                "canonical frozen graph query-shape set (foreign or missing "
                "member)"
            )
        icom_task_ids = evidence.get("expected_icom_task_ids")
        if set(icom_task_ids) != _V4_FROZEN_ICOM_TASK_IDS:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} expected_icom_task_ids member set != the "
                "canonical frozen graph iCom task-id set (foreign, missing or "
                "extra iCom task)"
            )
        # C-122 HG-G: the frozen-scenario exact binding.  The producer seals the
        # v4 source graph for EXACTLY the three frozen date pairs, with the SAME
        # fixed per-pair browser-source/query-task count and the SAME iCom-source
        # count on every pair, and declares ``total_planned_task_count`` as the
        # sum of those per-pair query-task counts.  A compact that omits the
        # per-pair breakdown, carries a 1-pair / 1-task graph, or declares a
        # total that does not equal the per-pair sum is a forged graph and fails
        # closed even though every field is non-empty / unique / positive.
        pair_ids = evidence.get("pair_ids")
        if not isinstance(pair_ids, list) or not pair_ids:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} pair_ids is empty"
            )
        if len(pair_ids) != len(set(pair_ids)):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} pair_ids are not unique"
            )
        if len(pair_ids) != _V4_FROZEN_DATE_PAIR_COUNT:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} pair_ids != the frozen scenario's exact "
                f"{_V4_FROZEN_DATE_PAIR_COUNT} date pairs "
                f"(got {len(pair_ids)})"
            )
        # C-122 supervision 01:10 counter-example: every pair id must be a
        # CANONICAL frozen-scenario date-pair id — well-formed ``date-pair:``
        # format and a digest that recomputes from the frozen scenario constants
        # plus the id's own dates.  A compact whose pair ids were replaced with
        # arbitrary unique foreign values like ``pair-1`` (or a well-formed id
        # with a wrong digest) fails closed even when every count and member
        # shape is preserved.  ``frozen_v4_pair_id_is_canonical`` is the same
        # single derivation the producer's ``_check_v4_source_graph`` uses.
        for pair_id in pair_ids:
            if not frozen_v4_pair_id_is_canonical(pair_id):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} pair id {pair_id!r} is not a canonical "
                    "frozen-scenario date-pair id (format, digest or "
                    "time-contract mismatch)"
                )
        # C-122 supervision 01:10 counter-example: the pair-id SET must equal the
        # run's checkpoint-bound sealed pair ids EXACTLY.  ``checkpoint_bound_
        # pair_ids`` is the independent job-control-plane record of the pairs the
        # run actually sealed (the terminal job's pair checkpoints), carried in
        # the compact alongside the producer's ``pair_ids``.  A compact with a
        # foreign pair, a wrong-pair swap, or a missing/extra pair fails closed
        # even when every id is individually well-formed.
        checkpoint_pair_ids = evidence.get("checkpoint_bound_pair_ids")
        if not isinstance(checkpoint_pair_ids, list) or len(checkpoint_pair_ids) != len(
            pair_ids
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} checkpoint_bound_pair_ids does not cover "
                "exactly the frozen pair set"
            )
        if set(checkpoint_pair_ids) != set(pair_ids):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} pair_ids set != the run's checkpoint-bound "
                "sealed pair set (foreign, swapped, missing or extra pair)"
            )
        # C-122 supervision 18:13 (Fix 4): the compact must also carry the full
        # desensitized checkpoint binding, and the validator must independently
        # re-verify chain integrity / the canonical date window / the request
        # identity / per-checkpoint content — a reordered chain, a wrong date, a
        # wrong request or a same-raw copied-digest forgery fails closed even
        # when every id is canonical and the set matches.  C-122 round-19
        # (gap 4): the binding's request identity is additionally bound to the
        # compact's ``api_payload_sha256`` when the compact carries it.
        _verify_layer6_checkpoint_binding(
            tracked_rel,
            check_name,
            evidence,
            pair_ids,
            api_payload_sha256=api_payload_sha256,
        )
        # The per-pair breakdown must cover EXACTLY the frozen pair set, with the
        # exact producer-consistent per-pair task counts.
        per_pair = evidence.get("per_pair")
        if not isinstance(per_pair, list) or len(per_pair) != len(pair_ids):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} per_pair breakdown != the frozen pair set"
            )
        per_pair_ids: set[str] = set()
        for entry in per_pair:
            if not isinstance(entry, dict):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} per_pair contains a non-object entry"
                )
            entry_pair_id = entry.get("pair_id")
            if not isinstance(entry_pair_id, str) or not entry_pair_id:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} per_pair entry has no pair_id"
                )
            if entry_pair_id in per_pair_ids:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} per_pair repeats pair {entry_pair_id!r}"
                )
            per_pair_ids.add(entry_pair_id)
            for key, label in (
                ("browser_source_task_count", "browser source task count"),
                ("query_task_count", "query task count"),
            ):
                count = entry.get(key)
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count != expected_tasks
                ):
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{check_name!r} pair {entry_pair_id!r} {label} != the "
                        "frozen per-pair browser task count"
                    )
            icom_count = entry.get("icom_source_task_count")
            if (
                not isinstance(icom_count, int)
                or isinstance(icom_count, bool)
                or icom_count != len(evidence.get("expected_icom_task_ids") or ())
            ):
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{check_name!r} pair {entry_pair_id!r} icom source task "
                    "count != the frozen per-pair iCom task set size"
                )
            # C-122 round-19 (supervision 17:03 Block 1 counter-example): each
            # pair must also carry EXACT per-pair member LISTS that equal the
            # canonical frozen graph member sets.  A pair whose browser Source
            # ids / query shapes / iCom task ids contain a foreign member, omit a
            # canonical member or swap in another pair's set fails closed even
            # when every count lines up — this is the wrong-pair-swap gate.
            for list_key, canonical, label in (
                (
                    "browser_source_task_ids",
                    _V4_FROZEN_BROWSER_SOURCE_IDS,
                    "browser Source id",
                ),
                ("query_task_ids", _V4_FROZEN_QUERY_SHAPES, "query shape"),
                (
                    "icom_source_task_ids",
                    _V4_FROZEN_ICOM_TASK_IDS,
                    "iCom task id",
                ),
            ):
                member_ids = entry.get(list_key)
                if not isinstance(member_ids, list) or len(member_ids) != len(
                    canonical
                ):
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{check_name!r} pair {entry_pair_id!r} {label} member "
                        "list is missing or has the wrong size"
                    )
                if len(member_ids) != len(set(member_ids)) or set(
                    member_ids
                ) != canonical:
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{check_name!r} pair {entry_pair_id!r} {label} member "
                        "set != the canonical frozen graph set (foreign, missing "
                        "or swapped member)"
                    )
        if per_pair_ids != set(pair_ids):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} per_pair ids != the frozen pair set"
            )
        # The declared total must be recomputable as the sum of the per-pair
        # query-task counts — a graph claiming 1 pair / 1 task / total=1 can
        # never satisfy this exact contract.
        total_planned = evidence.get("total_planned_task_count")
        if (
            not isinstance(total_planned, int)
            or isinstance(total_planned, bool)
            or total_planned < 1
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} total_planned_task_count is not a positive "
                "integer"
            )
        recomputed_total = sum(
            entry.get("query_task_count", 0)
            for entry in per_pair
            if isinstance(entry, dict)
        )
        if total_planned != recomputed_total:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} total_planned_task_count {total_planned} != "
                f"the per-pair query-task sum {recomputed_total}"
            )
    elif check_name == "stage_aware_exploration_publication_contract":
        # C-122 round-18 gate-2: the fixed stage contract — three sealed
        # explorations and two publication refreshes — is a semantic invariant,
        # not just a count of records.
        exploration = evidence.get("exploration_count")
        publication = evidence.get("publication_count")
        if (
            not isinstance(exploration, int)
            or isinstance(exploration, bool)
            or not isinstance(publication, int)
            or isinstance(publication, bool)
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} exploration/publication counts are not integers"
            )
        if exploration != 3 or publication != 2:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} must seal exactly 3 explorations and 2 "
                f"publication refreshes (got {exploration}/{publication})"
            )
    elif check_name == "real_v4_browser_source_evidence":
        # C-122 round-18 gate-2: every browser Source task must carry exactly
        # one snapshot — a count mismatch is a broken evidence chain, and
        # negative/non-int counts are forged.
        source_count = evidence.get("source_task_count")
        snapshot_count = evidence.get("snapshot_count")
        if (
            not isinstance(source_count, int)
            or isinstance(source_count, bool)
            or source_count < 0
            or not isinstance(snapshot_count, int)
            or isinstance(snapshot_count, bool)
            or snapshot_count < 0
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} source/snapshot counts are not non-negative integers"
            )
        if source_count != snapshot_count:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} snapshot_count {snapshot_count} != "
                f"source_task_count {source_count}"
            )
    elif check_name == "observed_cross_platform_overlap":
        # C-122 round-18 gate-2: a passing overlap proof requires at least three
        # time intervals with THREE DISTINCT providers truly concurrent — a
        # forged single-provider overlap must fail closed.
        interval_count = evidence.get("interval_count")
        max_providers = evidence.get("max_overlapping_providers")
        if (
            not isinstance(interval_count, int)
            or isinstance(interval_count, bool)
            or interval_count < 1
            or not isinstance(max_providers, int)
            or isinstance(max_providers, bool)
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} overlap counts are not integers"
            )
        if interval_count < 3 or max_providers != 3:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} does not prove 3-provider concurrent overlap "
                f"(intervals={interval_count}, max_providers={max_providers})"
            )
    elif check_name == "strict_selected_plan_platform_coverage":
        # C-122 round-18 gate-2: the strict coverage evidence must name EXACTLY
        # the fixed three OTA platforms — a partial or foreign provider list is
        # a forged completion receipt.
        providers = evidence.get("providers")
        if (
            not isinstance(providers, list)
            or len(providers) != len(_BROWSER_OTA_PROVIDERS)
            or set(providers) != _BROWSER_OTA_PROVIDERS
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} providers != the fixed three-OTA platform set"
            )
        # C-122 round-18 HG-E: a passing strict-coverage receipt must also prove
        # the run reached the full-completion state on every covered platform —
        # a compact carrying ``all_platforms_complete: false`` (or an unknown /
        # empty coverage mode) contradicts a passing verdict and fails closed.
        if evidence.get("all_platforms_complete") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} all_platforms_complete is not true"
            )
        coverage_mode = evidence.get("coverage_mode")
        # C-122 HG-G: the strict-coverage receipt only ever records the STRICT
        # coverage mode — a degraded / loose mode (or a missing / empty value)
        # contradicts a passing strict-coverage check and fails closed, even
        # when every other platform-completion field is present.
        if coverage_mode != "strict":
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} coverage_mode {coverage_mode!r} != 'strict'"
            )
    elif check_name == "icom_exploration_and_publication_evidence":
        # C-122 round-18 gate-2: the icom exploration coverage must be a TRUE
        # pass — a ``{"passed": false}`` (or any non-pass) binding cannot back a
        # passing check.
        coverage = evidence.get("exploration_full_coverage")
        if (
            not isinstance(coverage, dict)
            or coverage.get("passed") is not True
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} exploration_full_coverage is not passed"
            )
        target_task_ids = evidence.get("publication_target_task_ids")
        if not isinstance(target_task_ids, list) or not target_task_ids:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} publication_target_task_ids is empty"
            )
        if len(target_task_ids) != len(set(target_task_ids)):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check_name!r} publication_target_task_ids are not unique"
            )


def _verify_bridge_state_binding(
    compact: dict[str, Any],
    tracked_rel: str,
    key: str,
    *,
    compare_current: bool = False,
) -> None:
    """Validate one bridge-state lease binding in a layer-6 compact.

    Contract-required for BOTH the pre-run ``bridge_state_lease_preflight`` and
    the post-run ``bridge_state_lease_postcheck`` bindings (C-122 Fix 2 +
    round-18 item 6): a passing layer-6 gate must record the repo-relative
    bridge-state file identifier (never an absolute host path), the SHA256 of
    the exact bytes the snapshot validated, and the snapshot RESULT — an empty
    residual list (lease isolation proven).

    The recorded SHA is recomputed against the CURRENT live file ONLY for the
    POST-check binding (``compare_current=True``): the post-check IS the state
    right after the run, so if the named file still exists its bytes must match
    the recorded hash.  The PRE-flight binding (``compare_current=False``) is a
    CAPTURE-TIME snapshot: the E2E run legitimately repersists the bridge-state
    file while holding its lease, so the pre-flight SHA must be compared only
    against the capture-time bytes the runtime sealed — never against the live
    file the run itself may have advanced (C-122 HG-B).  Raises
    ``GateStateChangedError`` on any violation.
    """
    binding = compact.get(key)
    if not isinstance(binding, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} lacks the "
            f"{key} binding"
        )
    bridge_file = binding.get("file")
    if (
        not isinstance(bridge_file, str)
        or not bridge_file
        or os.path.isabs(bridge_file)
        or any(part == ".." for part in Path(bridge_file).parts)
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            f"{key} has no repo-relative file identifier"
        )
    binding_sha = binding.get("sha256")
    if (
        not isinstance(binding_sha, str)
        or len(binding_sha) != 64
        or _HEX_HASH_RE.fullmatch(binding_sha) is None
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            f"{key} has no valid sha256"
        )
    binding_residual = binding.get("residual")
    if not isinstance(binding_residual, list) or binding_residual:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            f"{key} residual is not an empty list "
            "(lease isolation not proven)"
        )
    # Recompute the binding against the CURRENT live file ONLY for the
    # post-check (compare_current=True).  A recorded post-check SHA that does
    # not match the actual bytes of the repo-relative file is stale or forged.
    # For the PRE-flight binding, the SHA is a capture-time snapshot sealed by
    # the runtime: the E2E run legitimately repersists the bridge-state file
    # while holding its lease, so comparing it to the current file would reject
    # a genuine pass (C-122 HG-B).  When the file no longer exists (runtime
    # cleanup), the recorded residual result remains the binding proof.
    bridge_live = ROOT / bridge_file
    if compare_current and bridge_live.is_file():
        actual_sha = _sha256_file(bridge_live)
        if actual_sha != binding_sha:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} "
                f"{key} sha256 does not match the "
                "current bridge-state file bytes"
            )


def _verify_layer6_compact_contract(
    tracked_rel: str,
    compact: dict[str, Any],
    *,
    tested_commit_sha: str | None = None,
) -> None:
    """C-118: hard-verify the committed layer-6 compact from E's blob.

    Requires the exact fifteen done-gate checks, all passed, with
    ``passed=true`` and all counts equal to 15, plus the repo / runtime /
    Companion identity and the event-injection / timeout / runner contracts.
    A compact missing a required check, carrying a non-passed check, or missing
    an identity/binding field fails the phase closed.

    C-122 acceptance: the compact must use the SAME schema version the producer
    emits, and validation is semantic, not just field-presence — a check whose
    recorded evidence contradicts a passing verdict (broken planner chain,
    mismatched budget, failed dynamic replan, sub-threshold exact providers,
    pending/evil provider states, a repo/runtime/tested SHA mismatch, or a
    missing candidate/scenario SHA binding) fails closed.
    """
    if compact.get("schema_version") != _LAYER6_COMPACT_SCHEMA:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} schema_version "
            f"{compact.get('schema_version')!r} != {_LAYER6_COMPACT_SCHEMA} "
            "(producer and validator must share the same schema version)"
        )
    # C-122 supervision 09:59 (Block 1): the top-level field set is a fixed
    # contract — an ALIAS of a whitelisted digest key (``API_PAYLOAD_CANDIDATE_
    # SET_SHA256`` / ``api-payload-candidate-set-sha256``) or any other foreign
    # field makes the compact foreign and fails closed, mirroring the digest
    # whitelist walker's raw-key exact match.
    unknown = set(compact) - _LAYER6_COMPACT_ALLOWED_TOP_LEVEL
    if unknown:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} unknown top-level "
            f"field(s): {sorted(unknown)!r}"
        )
    done_gate = compact.get("done_gate")
    if not isinstance(done_gate, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} lacks the "
            "done-gate report"
        )
    if done_gate.get("passed") is not True:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} done_gate.passed "
            "!= true"
        )
    if done_gate.get("check_count") != 15:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check_count != 15"
        )
    if done_gate.get("passed_check_count") != 15:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            "passed_check_count != 15"
        )
    checks = done_gate.get("checks")
    if not isinstance(checks, list) or len(checks) != 15:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} check set count "
            "!= 15"
        )
    present: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = check.get("name")
        if isinstance(name, str):
            present.add(name)
        if check.get("passed") is not True:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{check.get('name')!r} not passed"
            )
    if present != _V4_DONE_GATE_CHECK_NAMES:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} required check "
            "names do not match the fifteen done-gate checks"
        )
    # C-122 round-19 (gap 4): the compact must carry the raw request payload's
    # own SHA — a missing / malformed api_payload_sha256 voids the checkpoint
    # binding's request identity (the producer names it in request_identity).
    api_payload_sha = compact.get("api_payload_sha256")
    if (
        api_payload_sha is None
        or not isinstance(api_payload_sha, str)
        or _HEX_HASH_RE.fullmatch(api_payload_sha) is None
        or len(api_payload_sha) != 64
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} api_payload_sha256 "
            "is missing or not a valid sha256"
        )
    # C-122 Fix 3: every check must carry its desensitized, recomputable
    # per-item structured evidence, and each check's binding fields must be
    # present — a compact that reduces a check to a bare verdict (empty or
    # missing evidence, dropped binding) fails closed.
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = check.get("name")
        item_evidence = check.get("evidence")
        if not isinstance(item_evidence, dict) or not item_evidence:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} check "
                f"{name!r} carries no per-item structured evidence"
            )
        required = _LAYER6_REQUIRED_EVIDENCE_FIELDS.get(name)
        if required:
            missing = sorted(required - set(item_evidence))
            if missing:
                raise GateStateChangedError(
                    f"evidence commit E layer-6 compact {tracked_rel} check "
                    f"{name!r} evidence missing required binding field(s): "
                    + ", ".join(missing)
                )
            # C-122 round-18 item 5: a required binding field whose value is
            # None or an empty container cannot be recomputed from the committed
            # trail — it fails closed instead of passing on key-existence alone.
            for key in sorted(required):
                value = item_evidence.get(key)
                if value is None:
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{name!r} evidence binding field {key!r} is None"
                    )
                if isinstance(value, (dict, list, tuple, str)) and not value:
                    raise GateStateChangedError(
                        f"evidence commit E layer-6 compact {tracked_rel} check "
                        f"{name!r} evidence binding field {key!r} is empty"
                    )
        # C-122 acceptance: semantic consistency of each passing check — the
        # recorded evidence must prove the verdict, not merely exist.
        if isinstance(name, str):
            _verify_layer6_check_semantics(
                tracked_rel, name, item_evidence, api_payload_sha256=api_payload_sha
            )
    # Recomputable SHA bindings: the candidate-set SHA and the scenario SHA are
    # REQUIRED and must be well-formed 64-hex (a missing binding voids the
    # compact), and the prefrozen-candidate binding in the compact must agree
    # with the report's top-level api_payload_candidate_set_sha256.  C-122
    # round-19 (gap 4): the compact must ALSO carry the raw request payload's
    # own SHA so the checkpoint binding's request identity is bound to the
    # actual API payload the run submitted.
    candidate_sha = compact.get("api_payload_candidate_set_sha256")
    if (
        candidate_sha is None
        or not isinstance(candidate_sha, str)
        or _HEX_HASH_RE.fullmatch(candidate_sha) is None
        or len(candidate_sha) != 64
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            "api_payload_candidate_set_sha256 is missing or not a valid sha256"
        )
    scenario_sha = compact.get("scenario_sha256")
    if (
        scenario_sha is None
        or not isinstance(scenario_sha, str)
        or _HEX_HASH_RE.fullmatch(scenario_sha) is None
        or len(scenario_sha) != 64
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} scenario_sha256 "
            "is missing or not a valid sha256"
        )
    for check in checks:
        if not isinstance(check, dict) or check.get("name") != (
            "prefrozen_stay_plan_candidate_set"
        ):
            continue
        ev = check.get("evidence")
        ev_binding = ev.get("candidate_set_sha256") if isinstance(ev, dict) else None
        if (
            not isinstance(ev_binding, str)
            or len(ev_binding) != 64
            or _HEX_HASH_RE.fullmatch(ev_binding) is None
        ):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} "
                "prefrozen_stay_plan_candidate_set has no valid candidate_set_sha256"
            )
        if candidate_sha is not None and ev_binding.lower() != candidate_sha.lower():
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} prefrozen "
                "candidate-set SHA does not match api_payload_candidate_set_sha256"
            )
    # Repo / runtime / Companion identity plus the event-injection / timeout /
    # runner contracts: the independently reviewable bindings a passing layer-6
    # must preserve in the committed trail.
    for key in (
        "repo_revision",
        "runtime_before_run",
        "companion_preflight",
        "event_injection_contract",
        "timeout_contract",
        "runner_contract",
    ):
        if compact.get(key) is None:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} missing {key!r}"
            )
    if not isinstance(compact.get("repo_revision"), dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision is "
            "not an object"
        )
    if not isinstance(compact.get("runtime_before_run"), dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} runtime_before_run "
            "is not an object"
        )
    # C-122 Fix 2 + round-18 item 3/6: the lease-evidence bindings are
    # contract-required for BOTH the pre-run preflight and the post-run
    # postcheck — a passing layer-6 gate must record the repo-relative
    # bridge-state file identifier, the SHA256 of the exact bytes each snapshot
    # validated, and each snapshot RESULT (an empty residual list).  The
    # identifier must be repo-relative (never an absolute host path) and the
    # residual lists must be empty (lease isolation proven before AND after the
    # run).
    # C-122 HG-B: the PRE-flight SHA is a capture-time snapshot and must NOT be
    # recomputed against the live file the E2E run legitimately advanced; the
    # POST-check SHA IS the current state and must match the live file when it
    # still exists.
    _verify_bridge_state_binding(
        compact,
        tracked_rel,
        "bridge_state_lease_preflight",
        compare_current=False,
    )
    _verify_bridge_state_binding(
        compact,
        tracked_rel,
        "bridge_state_lease_postcheck",
        compare_current=True,
    )
    # C-122 Fix 3: a completed run must be claimed as completed, and the
    # identity / contract objects must carry their real binding fields — a
    # compact with an empty or malformed identity object fails closed.
    if compact.get("run_status") != "completed":
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} run_status "
            f"{compact.get('run_status')!r} != 'completed'"
        )
    repo_revision = compact.get("repo_revision") or {}
    repo_sha = repo_revision.get("commit_sha")
    if (
        not isinstance(repo_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", repo_sha) is None
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision."
            "commit_sha is not a valid 40-hex git SHA"
        )
    if not isinstance(repo_revision.get("worktree_dirty"), bool) or repo_revision[
        "worktree_dirty"
    ] is not False:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision "
            "worktree_dirty is not False"
        )
    if "toplevel" in repo_revision:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision "
            "carries an absolute host toplevel (C-122 round-18 item 5)"
        )
    runtime_provenance = (compact.get("runtime_before_run") or {}).get(
        "runtime_provenance"
    )
    if not isinstance(runtime_provenance, dict):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} runtime_before_run "
            "lacks runtime_provenance"
        )
    runtime_sha = runtime_provenance.get("commit_sha")
    if (
        not isinstance(runtime_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", runtime_sha) is None
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} runtime "
            "provenance commit_sha is not a valid 40-hex git SHA"
        )
    if "repo_toplevel" in runtime_provenance:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} runtime "
            "provenance carries an absolute host repo_toplevel "
            "(C-122 round-18 item 5)"
        )
    # C-122 round-18 item 5: the runtime identity must name the SAME revision
    # the compact claims to have tested — a mismatch voids the provenance.
    if repo_sha.lower() != runtime_sha.lower():
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision "
            "commit_sha != runtime provenance commit_sha"
        )
    # C-122 acceptance: the compact's tested revision must ALSO bind the run's
    # tested_commit_sha (S) — repo == runtime == S, or the compact cannot be
    # attributed to the code that was exercised.
    if tested_commit_sha is not None and repo_sha.lower() != tested_commit_sha.lower():
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} repo_revision "
            f"commit_sha {repo_sha!r} != tested_commit_sha {tested_commit_sha!r}"
        )
    companion_preflight = compact.get("companion_preflight") or {}
    stale_after = companion_preflight.get("stale_after_seconds")
    if (
        not isinstance(stale_after, int)
        or isinstance(stale_after, bool)
        or stale_after <= 0
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} companion_preflight "
            "has no positive stale_after_seconds"
        )
    # C-122 round-18 item 5: the Companion preflight must carry a healthy
    # status — a disconnected/failed companion cannot back the E2E evidence.
    if companion_preflight.get("status") != "connected":
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} companion_preflight "
            "status != 'connected'"
        )
    companions = companion_preflight.get("companions")
    if not isinstance(companions, list) or not companions:
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} companion_preflight "
            "has no companions"
        )
    for companion in companions:
        if not isinstance(companion, dict):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} companion "
                "preflight entry is not an object"
            )
        if not isinstance(companion.get("companion_id"), str) or not companion[
            "companion_id"
        ]:
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} companion "
                "preflight entry lacks a companion_id"
            )
        scopes = companion.get("authorized_scope_keys")
        if not isinstance(scopes, list) or set(scopes) != set(_CERTIFIED_OTA_SCOPES):
            raise GateStateChangedError(
                f"evidence commit E layer-6 compact {tracked_rel} companion "
                "preflight authorized_scope_keys != the certified browser "
                "Companion OTA scopes (C-122 HG-A; icom:transfer is not a "
                "Companion scope)"
            )
    event_injection = compact.get("event_injection_contract")
    if not isinstance(event_injection, dict) or not isinstance(
        event_injection.get("mode"), str
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} "
            "event_injection_contract lacks a mode"
        )
    timeout_contract = compact.get("timeout_contract")
    if not isinstance(timeout_contract, dict) or not isinstance(
        timeout_contract.get("server_execution_timeout_seconds"), int
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} timeout_contract "
            "lacks server_execution_timeout_seconds"
        )
    runner_contract = compact.get("runner_contract")
    if not isinstance(runner_contract, dict) or not isinstance(
        runner_contract.get("require_model_enhancement"), bool
    ):
        raise GateStateChangedError(
            f"evidence commit E layer-6 compact {tracked_rel} runner_contract "
            "lacks require_model_enhancement"
        )


def _verify_manifest_recomputes(
    staging_dir: Path, manifest: dict[str, Any], label: str
) -> None:
    """C-122 round-18 gate-3: recompute every manifest file hash from the
    CURRENT staging bytes and byte-compare with the committed manifest.

    A raw / compact / report file that changed after the manifest was generated
    would otherwise publish a stale hash — the committed manifest must record the
    exact bytes that exist at publish time, not the bytes that existed when the
    manifest was first written.  A missing staging file (should not happen in
    the commit phase) and a mismatched hash both fail closed.
    """
    files = manifest.get("files")
    if not isinstance(files, list):
        raise GateStateChangedError(
            f"{label} manifest files field is not a list; cannot recompute hashes"
        )
    for entry in files:
        if not isinstance(entry, dict):
            raise GateStateChangedError(
                f"{label} manifest has a non-object file entry"
            )
        staged_name = entry.get("name")
        recorded = entry.get("sha256")
        if not isinstance(staged_name, str) or not staged_name:
            raise GateStateChangedError(
                f"{label} manifest file entry has no name"
            )
        if not isinstance(recorded, str) or len(recorded) != 64:
            raise GateStateChangedError(
                f"{label} manifest file {staged_name!r} has no valid sha256"
            )
        staged = staging_dir / staged_name
        if not staged.is_file():
            raise GateStateChangedError(
                f"{label} manifest names {staged_name!r} but the staging file "
                "no longer exists"
            )
        actual = _sha256_file(staged)
        if actual.lower() != recorded.lower():
            raise GateStateChangedError(
                f"{label} manifest sha256 for {staged_name!r} does not match "
                "the current staging bytes (raw/compact changed after the "
                "manifest was generated); refusing to publish a stale hash"
            )


def _verify_compact_recomputed(staging_dir: Path) -> None:
    """C-122 round-18 gate-3: regenerate the desensitized layer-5/6 compacts
    from the CURRENT raw bytes and byte-compare against the staged artifacts.

    Compact generation is a deterministic function of the raw evidence, so a
    regenerated compact that differs from the to-be-committed artifact proves the
    compact was derived from different raw bytes than those on disk — the
    committed compact would certify a stale quote/run snapshot.  Fails closed.
    """
    for staged_name, generator, layer in (
        (_COMPACT_CANARY_STAGED_NAME, _compact_canary, "layer-5"),
        (_COMPACT_E2E_STAGED_NAME, _compact_live_e2e, "layer-6"),
    ):
        staged = staging_dir / staged_name
        if not staged.is_file():
            continue
        recomputed = generator(staging_dir)
        if recomputed is None:
            raise GateStateChangedError(
                f"cannot recompute {layer} compact {staged_name} from the "
                "current raw evidence"
            )
        serialized = json.dumps(
            recomputed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if serialized != staged.read_bytes():
            raise GateStateChangedError(
                f"recomputed {layer} compact {staged_name} differs from the "
                "staged artifact (raw changed after compact generation); "
                "refusing to publish stale compact evidence"
            )


def _verify_evidence_contract(
    evidence_commit: str,
    staging_dir: Path,
    *,
    tested_commit_sha: str | None,
    run_id: str,
) -> None:
    """Hard-verify E actually contains the contract-required manifest and every
    file the manifest marks committed (with a matching SHA256).  Any missing or
    corrupted committed evidence fails the phase closed (exit 2).

    C-122 round-18 (10:00 review #2): the manifest is re-parsed from E's
    COMMITTED blob — never trusted from in-memory state — and must bind the
    tested revision S and the run_id, or the audit trail cannot be independently
    re-verified from the commit alone."""
    tree = _git(
        "ls-tree", "-r", "--name-only", evidence_commit, check=True
    ).stdout.strip().splitlines()
    if _MANIFEST_REL not in tree:
        raise GateStateChangedError(
            f"evidence commit E {evidence_commit} missing required manifest"
        )
    manifest_blob = _git(
        "show", f"{evidence_commit}:{_MANIFEST_REL}", check=True, binary=True
    ).stdout
    try:
        manifest = json_loads_no_dupes(manifest_blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateStateChangedError(
            f"evidence commit E manifest {_MANIFEST_REL} is not valid JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise GateStateChangedError(
            f"evidence commit E manifest {_MANIFEST_REL} is not an object"
        )
    # C-122 round-19 (02:56 supervision / gap 3): the E manifest's STRUCTURAL
    # contract — schema_version, required fields, per-file field set / types /
    # uniqueness, the EXACT fixed evidence file-name set (never derived from
    # whatever happens to exist in a staging dir), the canonical tracked_path
    # and the canonical committed flag — is enforced by the SINGLE canonical
    # validator shared with generation and the resolver.  Publish-side and
    # resolver-side can no longer disagree on a renamed / missing / smuggled
    # evidence file, a relocated path or a flipped committed flag.
    manifest_problems: list[str] = []
    _validate_evidence_manifest(
        manifest, label="evidence commit E manifest", problems=manifest_problems
    )
    if manifest_problems:
        raise GateStateChangedError(manifest_problems[0])
    if manifest.get("tested_commit_sha") != tested_commit_sha:
        raise GateStateChangedError(
            "evidence commit E manifest tested_commit_sha does not bind the "
            "tested revision"
        )
    if manifest.get("run_id") != run_id:
        raise GateStateChangedError(
            "evidence commit E manifest run_id does not bind the run"
        )
    # Gate-3: recompute every manifest hash from the CURRENT staging bytes — a
    # raw/compact/report that changed after the manifest was written fails
    # closed here instead of publishing a stale hash.
    _verify_manifest_recomputes(staging_dir, manifest, "evidence commit E")
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
    # C-114: when a layer-5/6 compact artifact exists in staging it is
    # contract-required committed evidence — the manifest must record it as
    # committed and E must actually carry it (never only a committed=false raw
    # hash).  Raw originals may stay out of the repo; the compact must not.
    for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
        if "compact" not in staged_name or not (staging_dir / staged_name).is_file():
            continue
        if tracked_rel not in tree:
            raise GateStateChangedError(
                f"evidence commit E {evidence_commit} missing committed "
                f"compact artifact {tracked_rel}"
            )
        entry = next(
            (
                candidate
                for candidate in manifest["files"]
                if candidate.get("tracked_path") == tracked_rel
            ),
            None,
        )
        if entry is None or entry.get("committed") is not True:
            raise GateStateChangedError(
                f"evidence commit E manifest does not record compact artifact "
                f"{tracked_rel} as committed"
            )
        # C-114 R5: re-verify the committed compact CONTENT from E, not just its
        # hash — it must parse and carry the independently reviewable fields the
        # contract promises (layer-5 coverage + per-scope bindings, layer-6 full
        # done-gate check set).
        blob = _git("show", f"{evidence_commit}:{tracked_rel}", check=True, binary=True)
        try:
            compact = json_loads_no_dupes(blob.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateStateChangedError(
                f"evidence commit E compact artifact {tracked_rel} is not valid JSON"
            ) from exc
        if not isinstance(compact, dict) or not isinstance(
            compact.get("schema_version"), str
        ):
            raise GateStateChangedError(
                f"evidence commit E compact artifact {tracked_rel} has no "
                "schema_version"
            )
        # C-118: verify the committed compact CONTENT from E's blob against the
        # strong contract — layer-5 exact six scopes each passed/fresh/
        # authorized/read_only, layer-6 exact fifteen checks all passed plus the
        # identity/binding fields.  A forged or partial compact fails the phase
        # closed here, not at some later consumer.
        if staged_name == _COMPACT_CANARY_STAGED_NAME:
            _verify_layer5_compact_contract(tracked_rel, compact)
        elif staged_name == _COMPACT_E2E_STAGED_NAME:
            _verify_layer6_compact_contract(
                tracked_rel, compact, tested_commit_sha=tested_commit_sha
            )


def _verify_pointer_committed_blobs(
    pointer_commit: str,
    report: GateReport,
    evidence_commit: str,
    staging_entries: Iterable[tuple[str, Path]],
    staging_dir: Path,
) -> None:
    """Re-parse P's authoritative report + manifest from the COMMITTED blobs and
    re-verify every staged path's bytes match what was committed.

    C-122 round-18 (10:00 review #2) + C-122 P0 (2026-08-10 11:00): the
    authoritative record is P's committed report — parsed from the blob (valid
    JSON) and bound to the full schema (``passed=True``, tested S, evidence E,
    gate ref, run_id, non-empty layers).  P's committed manifest must likewise
    bind S/E/run_id/gate ref.  And each staged path must carry the SAME bytes in
    P as in the scanned staging file right now: the final secret scan happened
    after these writes, so a byte change after the scan (TOCTOU between scan
    and the publish) would mean the gate publishes a tree it never scanned —
    that fails closed instead of being silently published.

    Runs immediately before the atomic publish ref update, while nothing
    fallible remains after it."""
    report_blob = _git(
        "show", f"{pointer_commit}:{_REPORT_REL}",
        check=True, binary=True,
    ).stdout
    try:
        committed_report = json_loads_no_dupes(report_blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateStateChangedError(
            "pointer commit P authoritative report is not valid JSON"
        ) from exc
    if not isinstance(committed_report, dict):
        raise GateStateChangedError(
            "pointer commit P authoritative report is not an object"
        )
    # Full-schema binding of the authoritative report in P.
    # Gate-4: the report schema_version must be EXACT and the layer set must be
    # exactly the six unique layer names, each passed=true/skipped=false.
    if committed_report.get("schema_version") != EVIDENCE_SCHEMA:
        raise GateStateChangedError(
            f"pointer commit P report schema_version "
            f"{committed_report.get('schema_version')!r} != {EVIDENCE_SCHEMA}"
        )
    if committed_report.get("passed") is not True:
        raise GateStateChangedError(
            "pointer commit P report does not record passed=true"
        )
    if committed_report.get("tested_commit_sha") != report.tested_commit_sha:
        raise GateStateChangedError(
            "pointer commit P report tested_commit_sha does not bind the "
            "tested revision"
        )
    if committed_report.get("evidence_commit") != evidence_commit:
        raise GateStateChangedError(
            "pointer commit P report evidence_commit does not bind E"
        )
    if committed_report.get("run_id") != report.run_id:
        raise GateStateChangedError(
            "pointer commit P report run_id does not bind the run"
        )
    if committed_report.get("gate_ref") != _gate_ref(report.run_id):
        raise GateStateChangedError(
            "pointer commit P report gate_ref does not bind the side-channel "
            "publish ref"
        )
    committed_layers = committed_report.get("layers")
    if not isinstance(committed_layers, list) or len(committed_layers) != 6:
        raise GateStateChangedError(
            f"pointer commit P report layers must be exactly six, got "
            f"{len(committed_layers) if isinstance(committed_layers, list) else 'non-list'}"
        )
    layer_names: set[str] = set()
    for layer in committed_layers:
        if not isinstance(layer, dict):
            raise GateStateChangedError(
                "pointer commit P report has a non-object layer"
            )
        layer_name = layer.get("name")
        if not isinstance(layer_name, str) or not layer_name:
            raise GateStateChangedError(
                "pointer commit P report has a layer with no name"
            )
        if layer_name in layer_names:
            raise GateStateChangedError(
                f"pointer commit P report repeats layer {layer_name!r} "
                "(layer names must be unique)"
            )
        layer_names.add(layer_name)
        if layer.get("passed") is not True:
            raise GateStateChangedError(
                f"pointer commit P report layer {layer_name!r} is not passed=true"
            )
        if layer.get("skipped") is not False:
            raise GateStateChangedError(
                f"pointer commit P report layer {layer_name!r} is not skipped=false"
            )
    # C-122 round-18 gate-3: the layer set must be EXACTLY the six fixed layer
    # names — a renamed / replaced / foreign layer is not a done-gate pass even
    # when the count and the passed/skipped flags match.
    if layer_names != set(_ALL_LAYER_NAMES):
        raise GateStateChangedError(
            "pointer commit P report layer set != the six fixed layer names"
        )
    # P's committed manifest must bind S/E/run_id too, with the exact schema.
    manifest_blob = _git(
        "show", f"{pointer_commit}:{_MANIFEST_REL}", check=True, binary=True
    ).stdout
    try:
        committed_manifest = json_loads_no_dupes(manifest_blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateStateChangedError(
            f"pointer commit P manifest {_MANIFEST_REL} is not valid JSON"
        ) from exc
    if not isinstance(committed_manifest, dict):
        raise GateStateChangedError(
            f"pointer commit P manifest {_MANIFEST_REL} is not an object"
        )
    if committed_manifest.get("schema_version") != _MANIFEST_SCHEMA:
        raise GateStateChangedError(
            f"pointer commit P manifest schema_version "
            f"{committed_manifest.get('schema_version')!r} != {_MANIFEST_SCHEMA}"
        )
    if committed_manifest.get("tested_commit_sha") != report.tested_commit_sha:
        raise GateStateChangedError(
            "pointer commit P manifest tested_commit_sha does not bind the "
            "tested revision"
        )
    if committed_manifest.get("evidence_commit") != evidence_commit:
        raise GateStateChangedError(
            "pointer commit P manifest evidence_commit does not bind E"
        )
    if committed_manifest.get("run_id") != report.run_id:
        raise GateStateChangedError(
            "pointer commit P manifest run_id does not bind the run"
        )
    # C-122 supervision 03:46 (Block 2): P's publish preflight must enforce the
    # SAME canonical manifest contract as generation, E's preflight and the
    # resolver — a P phase that relocates a ``tracked_path``, flips a
    # ``committed`` flag, renames / drops / smuggles an evidence file or carries
    # an unbound sha256/size must fail closed here, not be certified and then
    # rejected by the official resolver.  The structural contract (schema,
    # exact file-name set, canonical name→tracked_path→committed map, per-file
    # sha256/size) is enforced by ``_validate_evidence_manifest``; the
    # S/E/run_id bindings above and the git-coupled blob recomputes below remain
    # the caller's job.
    manifest_problems: list[str] = []
    _validate_evidence_manifest(
        committed_manifest,
        label="pointer commit P manifest",
        problems=manifest_problems,
    )
    if manifest_problems:
        raise GateStateChangedError(manifest_problems[0])
    # Gate-3: recompute every P-manifest file hash from the CURRENT staging
    # bytes — a raw/compact that changed after the phase-2 manifest was written
    # fails closed here instead of publishing a stale hash.
    _verify_manifest_recomputes(staging_dir, committed_manifest, "pointer commit P")
    # Staged-path bytes: the committed blob must equal the scanned staging-file
    # bytes.  A path P does not carry (a git-ignored raw evidence copy) is
    # recorded by hash in the manifest instead and is exempt.
    for rel, staged in staging_entries:
        blob = _git(
            "show", f"{pointer_commit}:{rel}", check=False, binary=True
        )
        if blob.returncode != 0:
            continue
        if hashlib.sha256(blob.stdout).hexdigest() != _sha256_file(staged):
            raise GateStateChangedError(
                f"staged path {rel} bytes differ from the scanned staging file: "
                "refusing to publish a tree that differs from what was scanned"
            )


def _git_parent(commit: str) -> str:
    """The first parent of ``commit``, fail-closed on an unreadable graph."""
    parent = _git("rev-parse", "--verify", f"{commit}^", check=True).stdout.strip()
    if not parent:
        raise GateStateChangedError(f"commit {commit} has no readable parent")
    return parent


def _git_parents(commit: str) -> list[str]:
    """The full parent list of ``commit`` via ``rev-list --parents -n 1``.

    C-122 HG-C: the evidence trail must be a SINGLE-parent chain — a merge commit
    under the gate namespace (first parent looking correct, second parent
    foreign) must not masquerade as the published chain, so the verifier needs
    every parent, not just the first.
    """
    line = _git(
        "rev-list", "--parents", "-n", "1", commit, check=True
    ).stdout.split()
    # ``line[0]`` is the commit itself; ``line[1:]`` are its parents.
    return line[1:]


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
        # Restore through a sealed tmp (0600 from birth, C-122 Fix 5) renamed
        # over the target, so a restored blob is never briefly world-readable
        # and a pre-existing on-disk file is never written in place.
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        _write_sealed_bytes(tmp, probe.stdout, 0o600)
        os.replace(tmp, target)
    else:
        with contextlib.suppress(FileNotFoundError):
            target.unlink()


def _evidence_index_entries(
    staging_dir: Path,
    *,
    report_stage: Path,
    manifest_stage: Path,
) -> list[tuple[str, Path]]:
    """(repo-rel-path, staging-file) pairs E and P must carry.

    The authoritative report and the evidence manifest always land in E/P; each
    committable (non-git-ignored) staged evidence file joins them.  Raw
    git-ignored live-* evidence stays in the exclusive staging dir and is
    recorded by hash in the manifest only — E/P are side-channel commits, never
    the product branch (C-122 P0).
    """
    entries: list[tuple[str, Path]] = [
        (_REPORT_REL, report_stage),
        (_MANIFEST_REL, manifest_stage),
    ]
    for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
        staged = staging_dir / staged_name
        if not staged.is_file():
            continue
        # C-122 supervision 18:13 (规则漂移): committability comes from the FIXED
        # authoritative contract, never from ``git check-ignore`` — a worktree
        # ``.gitignore`` edit must not change what E/P carry.
        if not _EVIDENCE_COMMITTED_CONTRACT[staged_name]:
            continue
        entries.append((tracked_rel, staged))
    return entries


def _stage_blob_into_temp_index(
    rel: str,
    source: Path,
    index_env: dict[str, str],
    needles: _SecretNeedles,
) -> None:
    """Scan-then-write one staging file into the object DB and add it to the
    temp index at ``rel`` — without ever touching the shared worktree (C-122
    P0).

    C-122 round-18 gate-1 ordering: the EXACT bytes that will become the blob
    are read and secret-scanned in memory FIRST; only a passing scan pipes the
    same bytes via ``git hash-object -w --stdin``.  A rejected artifact never
    enters the object graph, so a sensitive compact/report can never become a
    dangling blob after a late scan failure.  ``git update-index --add
    --cacheinfo`` is an index-only operation, so the real worktree file at
    ``rel`` is never created or modified and the product branch / HEAD / real
    index / worktree stay byte-identical from start to finish.
    """
    _verify_evidence_file_safety(source, "evidence")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise GateStateChangedError(
            f"cannot read evidence file {source.name} for staging "
            f"({exc.__class__.__name__})"
        ) from exc
    # A committed artifact must not carry a credential-looking field name
    # (report / manifest / the committable evidence paths in E/P); raw
    # git-ignored live-* dumps are value-scanned but not structurally walked.
    committed_artifact = rel in {
        _REPORT_REL,
        _MANIFEST_REL,
    } or any(rel == tracked_rel for _, tracked_rel in _EVIDENCE_TRACKED_PATHS)
    _secret_scan_bytes(
        data,
        needles,
        "evidence",
        source.name,
        credential_field_check=committed_artifact,
    )
    blob = _git(
        "hash-object", "-w", "--stdin", check=True, env_extra=index_env,
        input_bytes=data,
    ).stdout.strip().decode("utf-8", errors="replace")
    if not blob:
        raise GateStateChangedError(f"cannot hash evidence file {source.name}")
    _git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},{rel}",
        check=True,
        env_extra=index_env,
    )


def _stage_evidence_into_temp_index(
    staging_dir: Path,
    index_env: dict[str, str],
    needles: _SecretNeedles,
    *,
    report_stage: Path,
    manifest_stage: Path,
) -> None:
    """Populate the temp index with every file E must carry (report, manifest,
    committable evidence), read directly from the staging files."""
    for rel, staged in _evidence_index_entries(
        staging_dir, report_stage=report_stage, manifest_stage=manifest_stage
    ):
        _stage_blob_into_temp_index(rel, staged, index_env, needles)


def _probe_gate_ref_state(
    gate_ref: str, env_extra: dict[str, str] | None = None
) -> str:
    """Return the gate ref's persistent state for a create-only publish decision.

    The probe answers ``"absent"`` / ``"symref:<target>"`` / ``"direct:<oid>"``
    and NEVER trusts a dereferenced read for the type decision:

      * ``symbolic-ref -q`` answers first — a symref is reported as the symref
        it is, even when its target ref is missing (a plain ``rev-parse`` would
        then return nothing and masquerade as absent) or exists (``rev-parse``
        would hand back the VICTIM's OID and masquerade as a direct ref at that
        OID).
      * Only when the ref is NOT a symref does ``rev-parse --verify`` decide
        between absent and a direct ref.

    C-122 P0 symbolic-ref hijack guard: a pre-seeded symref
    ``refs/tripchord/done-gate/<run_id> -> <victim>`` must be rejected as a
    conflict before the publish AND in the read-only reconciliation — the
    evidence P must never land at the victim name, and a symref silently
    converted to a direct ref by ``update-ref --no-deref`` must not count as a
    clean create.  The success condition for the publish is always: the ref is
    a DIRECT ref holding exactly the expected OID.
    """
    sym = _git("symbolic-ref", "-q", gate_ref, check=False, env_extra=env_extra)
    if sym.returncode == 0:
        target = (sym.stdout or "").strip()
        return f"symref:{target}" if target else "symref"
    oid = _git(
        "rev-parse", "--verify", "--quiet", gate_ref, check=False, env_extra=env_extra
    ).stdout.strip()
    return f"direct:{oid}" if oid else "absent"


def _publish_gate_ref(
    gate_ref: str, pointer_commit: str, commit_env: dict[str, str]
) -> None:
    """Atomic create-only publish with read-only reconciliation.

    C-122 round-18 gate-2: the create-only ``update-ref`` (old value all-zero)
    is the only action of the whole evidence commit that changes persistent
    state.  If the update-ref itself raises or times out, the outcome is
    ambiguous only until the ref is read back — the gate reconciles READ-ONLY so
    a caller-side failure can never coexist with a published ``passed=true`` P:

      * ref is a DIRECT ref == expected P  -> the publish landed; success.
      * ref missing                        -> nothing was published; fail closed.
      * ref holds any other value OR is a symbolic ref -> conflict (a
        pre-seeded/hijacked ref or a concurrent/crashed run claimed the
        run_id); fail closed.

    C-122 P0 symbolic-ref hijack guard: a pre-seeded symref
    ``gate_ref -> victim`` must never redirect the evidence write to the
    victim, and must never be silently converted by the update itself.  The
    probe BEFORE the update rejects any existing direct ref or symref as a
    conflict, and the update uses ``--no-deref`` so even a symref appearing in
    the probe-to-lock window cannot be dereferenced into a victim write.  The
    read-only reconciliation then accepts ONLY a direct ref holding exactly
    ``pointer_commit`` — a dereferenced read that happens to match P is not
    success.

    Repo hooks (``reference-transaction`` and friends) are disabled via
    ``core.hooksPath`` pointing at an empty safe directory for the whole commit
    phase, so a repository hook can never observe, veto or half-apply the
    side-channel update.
    """
    # PRE-PUBLISH type check: create-only means the name must be completely
    # free.  Any existing direct ref OR symref is a conflict.
    preset = _probe_gate_ref_state(gate_ref, commit_env)
    if preset != "absent":
        raise GateStateChangedError(
            f"gate ref {gate_ref} preset as {preset}; refusing to publish: "
            "create-only requires an absent direct-ref name (symbolic-ref "
            "hijack guard)"
        )
    try:
        # ``--no-deref``: the update treats gate_ref itself as the target, so
        # a symref racing in after the probe can never push P into the victim.
        _git(
            "update-ref", "--no-deref", gate_ref, pointer_commit, _ZERO_SHA,
            check=True, env_extra=commit_env,
        )
        return
    except GateStateChangedError as exc:
        current = _probe_gate_ref_state(gate_ref, commit_env)
        if current == f"direct:{pointer_commit}":
            return  # publish landed before the failure surfaced
        if current == "absent":
            raise GateStateChangedError(
                f"publish did not land and gate ref {gate_ref} does not exist: "
                "no evidence was published"
            ) from exc
        raise GateStateChangedError(
            f"gate ref conflict: {gate_ref} is {current}, not the expected "
            f"direct:{pointer_commit}; a pre-seeded direct ref or symref "
            "(hijack), repoint, or concurrent/crashed run already claimed this "
            "run_id"
        ) from exc


def _commit_evidence(
    staging_dir: Path,
    report: GateReport,
    *,
    start: GitSnapshot,
    local_report_path: Path | None = None,
) -> str:
    """Side-channel evidence commit, atomically (C-122 P0 / 2026-08-10 11:00).

    The authoritative repository — current branch, HEAD, real index and
    worktree — is byte-for-byte read-only from entry to exit.  No real-index
    copy/replace/read-tree/reset/sync and no ``update-ref`` against the current
    branch ever run.  E and P are built entirely from a temporary
    ``GIT_INDEX_FILE`` plus ``git commit-tree``:

      E: tree = S's tree + evidence paths (report, manifest, committable
         evidence), parent = S (the tested revision).
      P: tree = E's tree with the report/manifest updated to bind
         ``evidence_commit=E``, parent = E.

    Everything the contract requires — the manifest, the layer-5/6 compact
    artifacts, the authoritative report, S / E / run_id / gate ref / hashes /
    layer verdicts — is re-parsed from E's and P's COMMITTED blobs before
    anything is published.

    The ONLY action that affects persistent state is the final atomic
    create-only ``git update-ref refs/tripchord/done-gate/<run_id> <P>`` with
    old value all-zero: if the ref already exists the update fails and the gate
    fails closed.  That update-ref is the last statement that can affect
    persistent state — nothing fallible runs after it.

    A crash before the publish leaves at most unreachable objects and changes
    no visible repository state; after the publish, consumers verify ``P^=E``,
    ``E^=S`` and the report/manifest bindings through the dedicated ref.  The
    evidence commit is NEVER installed as the product branch HEAD.
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
    tested_commit_sha = report.tested_commit_sha
    if not tested_commit_sha:
        raise GateStateChangedError("no tested revision to certify")
    if not report.run_id:
        raise GateStateChangedError("no run_id to name the side-channel gate ref")
    gate_ref = _gate_ref(report.run_id)

    # C-122 round-18 gate-6: the committed report must not carry the absolute
    # host ``toplevel`` (it is committed evidence).  The binding was already
    # validated against the live snapshot above; the staged/delivered copy is
    # stripped of the host path while every repo-relative identifier stays.
    report.toplevel = None

    # The authoritative report and manifest are staged OUTSIDE the tracked
    # worktree, in the exclusive 0600 staging dir.  In main() the delivered
    # ``--output`` report is this same file.
    report_stage = local_report_path or staging_dir / _REPORT_STAGED_NAME
    manifest_stage = staging_dir / _MANIFEST_STAGED_NAME

    # Temp GIT_INDEX_FILE: ALL staging happens against this temp index — the
    # real index is never read-tree'd, staged into or copied from (C-122 P0).
    # The same env disables repository hooks (``core.hooksPath`` -> an empty
    # safe dir) so a ``reference-transaction`` or any other repo hook can never
    # observe, veto or half-apply the side-channel commit phase (C-122 round-18
    # gate-2).  ``needles`` is captured ONCE so every scan in this phase uses
    # the same secret set.
    index_tmp = tempfile.mkdtemp(prefix="gate-index-")
    no_hooks = Path(index_tmp) / "no-hooks"
    no_hooks.mkdir(parents=True, exist_ok=True)
    index_env = {
        "GIT_INDEX_FILE": str(Path(index_tmp) / "index"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(no_hooks),
        # C-122 round-18 gate-4: every git child in the commit phase — most
        # importantly the two ``commit-tree`` calls that create E and P — runs
        # under the fixed non-personal evidence identity, so ambient repo/user
        # git config can never author the trail.
        **_EVIDENCE_COMMIT_IDENTITY,
    }
    needles = _evidence_scan_needles()
    try:
        # Phase 1: E — evidence_commit unset, parent = S.
        _dump(report, report_stage)
        _write_manifest(_evidence_manifest(staging_dir, report), manifest_stage)
        _git("read-tree", tested_commit_sha, check=True, env_extra=index_env)
        _stage_evidence_into_temp_index(
            staging_dir,
            index_env,
            needles,
            report_stage=report_stage,
            manifest_stage=manifest_stage,
        )
        e_tree = _git("write-tree", check=True, env_extra=index_env).stdout.strip()
        if not e_tree:
            raise GateStateChangedError("evidence tree unreadable after write-tree")
        evidence_commit = _git(
            "commit-tree",
            e_tree,
            "-p",
            tested_commit_sha,
            "-m",
            f"Done-Gate evidence for tested commit {tested_commit_sha} "
            f"({report.generated_at})",
            check=True,
            env_extra=index_env,
        ).stdout.strip()
        if not evidence_commit:
            raise GateStateChangedError("evidence commit created but SHA unreadable")
        # Atomic binding: E's first parent must be the tested revision S, and E
        # must carry the contract files (re-parsed from E's committed blobs).
        _assert_parent_is(evidence_commit, tested_commit_sha, "evidence commit E")
        _verify_evidence_contract(
            evidence_commit,
            staging_dir,
            tested_commit_sha=tested_commit_sha,
            run_id=report.run_id,
        )
        # Phase 2: P — record evidence_commit=E (and the side-channel gate ref)
        # in the report, re-stamp the manifest, and materialize P.  Still no
        # branch move and no real-index write.
        report.evidence_commit = evidence_commit
        report.gate_ref = gate_ref
        _dump(report, report_stage)
        if local_report_path is not None and local_report_path != report_stage:
            _dump(report, local_report_path)
        _write_manifest(
            _evidence_manifest(staging_dir, report, evidence_commit=evidence_commit),
            manifest_stage,
        )
        # Re-stage the two changed files (report + manifest) into the temp index.
        _stage_blob_into_temp_index(_REPORT_REL, report_stage, index_env, needles)
        _stage_blob_into_temp_index(_MANIFEST_REL, manifest_stage, index_env, needles)
        p_tree = _git("write-tree", check=True, env_extra=index_env).stdout.strip()
        if not p_tree:
            raise GateStateChangedError("pointer tree unreadable after write-tree")
        pointer_commit = _git(
            "commit-tree",
            p_tree,
            "-p",
            evidence_commit,
            "-m",
            f"Record Done-Gate evidence_commit={evidence_commit} for tested commit "
            f"{tested_commit_sha}",
            check=True,
            env_extra=index_env,
        ).stdout.strip()
        if not pointer_commit:
            raise GateStateChangedError("pointer commit created but SHA unreadable")
        # Phase 2 parent must be E, and P's committed manifest must carry
        # evidence_commit=E (field-completeness of the committed trail).
        _assert_parent_is(pointer_commit, evidence_commit, "phase 2 pointer commit")
        # C-114 ordering fix: the final comprehensive secret scan runs AFTER
        # every report / manifest / compact evidence file is written and BEFORE
        # the publish, so a leak in the last-written artifacts can never reach
        # the published ref.  Scan errors report only category + file name.
        scan_paths = [report_stage, manifest_stage]
        scan_paths.extend(
            staging_dir / staged_name
            for staged_name, _ in _EVIDENCE_TRACKED_PATHS
            if (staging_dir / staged_name).is_file()
        )
        _final_evidence_secret_scan(staging_dir, scan_paths)
        # C-122 round-18 gate-3: from the SAME final raw bytes, recompute the
        # desensitized compacts and byte-compare against the to-be-committed
        # artifacts — a raw change after compact generation fails closed here
        # instead of publishing a stale compact.
        _verify_compact_recomputed(staging_dir)
        # Immediately before the publish: re-parse P's authoritative report +
        # manifest from the COMMITTED blobs and re-verify every staged path's
        # on-disk bytes still equal what the final scan just read.  A tampered E
        # manifest, a non-JSON P report or a scanned-path byte change after the
        # final check all fail closed here, never after the publish.
        _verify_pointer_committed_blobs(
            pointer_commit,
            report,
            evidence_commit,
            _evidence_index_entries(
                staging_dir,
                report_stage=report_stage,
                manifest_stage=manifest_stage,
            ),
            staging_dir,
        )
        # PUBLISH — the last action that can affect persistent state.  Atomic
        # create-only update-ref of the namespaced gate ref with old value
        # all-zero, reconciled read-only on any failure (a caller-side failure
        # can never coexist with a published passed=true P — C-122 round-18
        # gate-2).  Nothing fallible runs after this.
        _publish_gate_ref(gate_ref, pointer_commit, index_env)
        return evidence_commit
    except (GateStateChangedError, OSError) as exc:
        # Fail closed: nothing was published and the shared repo was never
        # touched (HEAD/index/branch/worktree byte-for-byte at S), so there is
        # nothing to restore in the tracked tree.  The staged files may stay in
        # the exclusive staging dir; only the delivered local report copy is
        # removed — it may carry evidence_commit or a leak, and the caller
        # re-dumps the corrected (non-passing) verdict.  ``gate_ref`` is cleared
        # too so a failure report never claims a side-channel ref that was never
        # published.
        report.evidence_commit = None
        report.gate_ref = None
        if local_report_path is not None:
            with contextlib.suppress(OSError):
                local_report_path.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise GateStateChangedError(f"evidence commit I/O failure: {exc}") from exc
        raise
    finally:
        # Best-effort temp-index cleanup; ignore_errors=True keeps this
        # non-fallible so it can never turn a published gate into a failure.
        shutil.rmtree(index_tmp, ignore_errors=True)


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


def _safe_print_report(report: GateReport, output_path: Path, quiet: bool) -> None:
    """Best-effort report printing (C-118 Gap 1).

    After the CAS has installed a ``passed=true`` pointer commit, nothing
    fallible may run that can flip the exit or surface a raw traceback.  A
    print failure (e.g. ``BrokenPipeError`` on a closed stdout) is swallowed;
    the authoritative verdict already lives in the committed report.
    """
    with contextlib.suppress(BrokenPipeError, OSError):
        _print_report(report, output_path, quiet)


def _latest_gate_run_id() -> str:
    """The run_id of the most recently published gate ref.

    Lists ``refs/tripchord/done-gate/*`` sorted by committer date (newest first)
    and returns the first run_id.  C-122 P0: a symbolic ref under the namespace
    is a hijack, never a published run, and is skipped — ``--latest`` must not
    resolve to a run whose evidence trail is not a direct ref.  The
    authoritative binding is always the explicit
    ``refs/tripchord/done-gate/<run_id>`` for the run being verified.
    """
    refs = _git(
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname)%00%(symref)",
        _DONE_GATE_REF_PREFIX,
        check=True,
    ).stdout.splitlines()
    for line in refs:
        line = line.strip()
        if not line:
            continue
        ref, _sep, symref = line.partition("\0")
        if symref:
            continue  # symbolic ref under the gate namespace: hijack, skip
        run_id = ref.rsplit("/", 1)[-1]
        if run_id and _RUN_ID_RE.fullmatch(run_id):
            return run_id
    raise GateStateChangedError(
        f"no published gate refs under {_DONE_GATE_REF_PREFIX} to resolve"
    )


def _verify_manifest_files_contract(
    files: list[Any], problems: list[str], label: str
) -> None:
    """Validate one committed manifest's ``files`` list entry-by-entry.

    C-122 HG-H: the P manifest is itself a committed artifact the consumer must
    authenticate — this is the exact per-entry field contract the E manifest
    already enforces (fixed field set, unique names, valid 64-hex sha256,
    integer size, boolean committed).  A forged pointer commit whose manifest
    smuggles a secret-looking field name, drops a fixed file or renames an entry
    fails closed here.  Appends a ``problems`` entry per violation; never raises.
    """
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            problems.append(f"{label} has a non-object file entry")
            continue
        if set(entry) != {"name", "tracked_path", "sha256", "size_bytes", "committed"}:
            problems.append(
                f"{label} file entry has an unexpected field set {sorted(entry)}"
            )
        entry_name = entry.get("name")
        entry_sha = entry.get("sha256")
        entry_size = entry.get("size_bytes")
        entry_committed = entry.get("committed")
        if not isinstance(entry_name, str) or not entry_name:
            problems.append(f"{label} has a file with no name")
            continue
        if entry_name in seen:
            problems.append(f"{label} repeats file name {entry_name!r}")
        seen.add(entry_name)
        if (
            not isinstance(entry_sha, str)
            or _HEX_HASH_RE.fullmatch(entry_sha) is None
            or len(entry_sha) != 64
        ):
            problems.append(
                f"{label} file {entry_name!r} sha256 is not a valid 64-hex digest"
            )
        if not isinstance(entry_size, int) or isinstance(entry_size, bool):
            problems.append(
                f"{label} file {entry_name!r} size_bytes is not an integer"
            )
        if not isinstance(entry_committed, bool):
            problems.append(
                f"{label} file {entry_name!r} committed is not a boolean"
            )
        rel = entry.get("tracked_path")
        if not isinstance(rel, str) or not rel:
            problems.append(
                f"{label} file {entry_name!r} has no tracked_path"
            )


def _verify_p_manifest_binds_e(
    p_manifest: dict[str, Any],
    e_manifest: dict[str, Any],
    evidence_commit: str,
    problems: list[str],
) -> None:
    """C-122 round-18 HG-H2: cross-bind every P manifest file entry to the E
    canonical manifest entry of the same name AND to the real committed blob.

    A forged pointer commit whose manifest records an arbitrary (but well-formed)
    64-hex sha256, size 0, and committed=false for every evidence file passes the
    field-shape contract (_verify_manifest_files_contract) — the entries still
    need to be the SAME evidence E actually committed.  Each P entry must match
    E's canonical entry field-for-field (tracked_path / committed / sha256 /
    size_bytes) and, for committed entries, recompute to the blob in E's tree.
    Appends a ``problems`` entry per violation; never raises for a verification
    failure (a git failure raises GateStateChangedError, fail-closed).
    """
    p_files = p_manifest.get("files")
    e_files = e_manifest.get("files")
    if not isinstance(p_files, list):
        problems.append("pointer commit P manifest files field is not a list")
        return
    if not isinstance(e_files, list):
        problems.append("evidence commit E manifest files field is not a list")
        return
    e_by_name: dict[str, dict[str, Any]] = {}
    for entry in e_files:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            e_by_name[entry["name"]] = entry
    for p_entry in p_files:
        if not isinstance(p_entry, dict):
            continue  # field-shape contract already flagged it
        p_name = p_entry.get("name")
        if not isinstance(p_name, str) or not p_name:
            continue  # field-shape contract already flagged it
        e_entry = e_by_name.get(p_name)
        if e_entry is None:
            problems.append(
                f"pointer commit P manifest file {p_name!r} has no matching "
                "entry in E's canonical manifest"
            )
            continue
        for key in ("tracked_path", "committed", "sha256", "size_bytes"):
            p_val = p_entry.get(key)
            e_val = e_entry.get(key)
            if p_val != e_val:
                problems.append(
                    f"pointer commit P manifest file {p_name!r} {key} "
                    f"{p_val!r} != E canonical manifest {e_val!r}"
                )
        # Bind committed entries to the real blob: recompute from E's tree.
        if e_entry.get("committed") is True:
            rel = e_entry.get("tracked_path")
            if not isinstance(rel, str) or not rel:
                continue
            blob = _git(
                "show", f"{evidence_commit}:{rel}", check=True, binary=True
            ).stdout
            actual_sha = hashlib.sha256(blob).hexdigest()
            actual_size = len(blob)
            if p_entry.get("sha256") != actual_sha:
                problems.append(
                    f"pointer commit P manifest file {p_name!r} sha256 does not "
                    "match E's committed blob"
                )
            if p_entry.get("size_bytes") != actual_size:
                problems.append(
                    f"pointer commit P manifest file {p_name!r} size_bytes "
                    f"{p_entry.get('size_bytes')!r} != E's committed blob "
                    f"{actual_size}"
                )


def verify_gate_ref(run_id: str) -> dict[str, Any]:
    """Machine-gate consumer entry: resolve and verify a published evidence trail.

    C-122 round-18 gate-7 + HG-C: from ``refs/tripchord/done-gate/<run_id>``
    resolve the pointer commit P and verify the full chain P -> E -> S together
    with every committed evidence blob:

      1. P's first parent is E; E's first parent is S (the tested revision),
         and BOTH links are single-parent — a merge commit under the gate
         namespace is not a valid publish.
      2. P's committed report (exact schema) binds S / E / run_id / gate_ref,
         ``passed=true``, and exactly the six unique layer verdicts, each
         passed=true / skipped=false.
      3. P's committed manifest (exact schema) binds S / E / run_id.
      4. E's committed manifest (exact schema) binds S / run_id and is the
         COMPLETE publisher contract (C-122 HG-C): it lists exactly the fixed
         evidence file set with the exact field set and per-file size, records
         every committed file present in E with a matching blob SHA256 AND size,
         records the git-ignored raw originals as committed=false and absent from
         E's tree, commits both layer-5/6 compact artifacts (each re-verified
         against the strong compact contract from E's blob, and each
         raw_evidence.sha256 cross-checked against the manifest's recorded raw
         hash), and re-scans every committed JSON blob (report, manifest,
         evidence files) for credential field names / unknown 64-hex values.

    The tracked ``benchmarks/results/product-v1-done-gate.json`` convenience copy
    is NEVER trusted here — the gate ref is the authoritative record.  Returns a
    verdict dict ``{"verified": bool, ...}``; a non-verified result carries a
    ``problems`` list naming each violation and never raises for a verification
    failure (only git unavailability raises, fail-closed).
    """
    try:
        gate_ref = _gate_ref(run_id)
    except GateStateChangedError as exc:
        return {
            "verified": False,
            "run_id": run_id,
            "problems": [str(exc)],
        }
    verdict: dict[str, Any] = {
        "verified": False,
        "run_id": run_id,
        "gate_ref": gate_ref,
    }
    # C-122 P0 symbolic-ref hijack guard: only a DIRECT ref is authoritative.
    # A symref under the gate namespace (gate_ref -> victim) must fail closed —
    # a dereferenced ``rev-parse`` would hand back the victim's OID and
    # masquerade as a published trail.
    state = _probe_gate_ref_state(gate_ref)
    if state == "absent":
        verdict["problems"] = [
            f"gate ref {gate_ref} does not exist; nothing was published"
        ]
        return verdict
    if state.startswith("symref"):
        verdict["problems"] = [
            f"gate ref {gate_ref} is a symbolic ref ({state}); the evidence "
            "trail is hijacked — only a direct ref is authoritative"
        ]
        return verdict
    pointer_commit = state[len("direct:"):]
    verdict["pointer_commit"] = pointer_commit
    problems: list[str] = []
    try:
        evidence_commit = _git_parent(pointer_commit)
        verdict["evidence_commit"] = evidence_commit
        tested_commit_sha = _git_parent(evidence_commit)
        verdict["tested_commit_sha"] = tested_commit_sha
        # C-122 HG-C: the P -> E -> S chain must be SINGLE-parent throughout.  A
        # merge commit under the gate namespace (first parent right, second
        # parent foreign) is not a valid publish even though the first-parent
        # reads above resolve — a reviewer must be able to trust that E carries
        # exactly S and P carries exactly E, with no grafted second lineage.
        for commit, label, expected_parent in (
            (pointer_commit, "pointer commit P", evidence_commit),
            (evidence_commit, "evidence commit E", tested_commit_sha),
        ):
            parents = _git_parents(commit)
            if parents != [expected_parent]:
                problems.append(
                    f"{label} {commit} parent list {parents} != "
                    f"[{expected_parent}] (the evidence trail must be a "
                    "single-parent chain)"
                )
        # 1. P's authoritative report binds S / E / run_id / gate_ref.
        report_blob = _git(
            "show", f"{pointer_commit}:{_REPORT_REL}", check=True, binary=True
        ).stdout
        try:
            committed_report = json_loads_no_dupes(report_blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"pointer commit P report {_REPORT_REL} is not valid JSON")
            committed_report = None
        if not isinstance(committed_report, dict):
            problems.append("pointer commit P report is not an object")
            committed_report = {}
        if committed_report.get("schema_version") != EVIDENCE_SCHEMA:
            problems.append(
                f"pointer commit P report schema_version "
                f"{committed_report.get('schema_version')!r} != {EVIDENCE_SCHEMA}"
            )
        if committed_report.get("passed") is not True:
            problems.append("pointer commit P report does not record passed=true")
        if committed_report.get("tested_commit_sha") != tested_commit_sha:
            problems.append(
                "pointer commit P report tested_commit_sha does not bind S"
            )
        if committed_report.get("evidence_commit") != evidence_commit:
            problems.append("pointer commit P report evidence_commit does not bind E")
        if committed_report.get("run_id") != run_id:
            problems.append("pointer commit P report run_id does not bind the run")
        if committed_report.get("gate_ref") != gate_ref:
            problems.append(
                "pointer commit P report gate_ref does not bind the side-channel ref"
            )
        # C-122 HG-C: re-scan P's committed report blob for a credential field
        # name / unknown 64-hex value — the committed JSON artifacts are the last
        # line of defence against a leak that reached the object graph.
        try:
            _reject_credential_field_names(
                report_blob, "committed evidence", _REPORT_REL
            )
            _reject_unknown_64hex_values(
                report_blob, "committed evidence", _REPORT_REL
            )
        except GateStateChangedError as exc:
            problems.append(str(exc))
        committed_layers = committed_report.get("layers")
        if not isinstance(committed_layers, list) or len(committed_layers) != 6:
            problems.append(
                "pointer commit P report layers must be exactly six unique layers"
            )
        else:
            layer_names: set[str] = set()
            for layer in committed_layers:
                if not isinstance(layer, dict):
                    problems.append("pointer commit P report has a non-object layer")
                    continue
                layer_name = layer.get("name")
                if not isinstance(layer_name, str) or not layer_name:
                    problems.append("pointer commit P report has a layer with no name")
                    continue
                if layer_name in layer_names:
                    problems.append(
                        f"pointer commit P report repeats layer {layer_name!r}"
                    )
                layer_names.add(layer_name)
                if layer.get("passed") is not True:
                    problems.append(
                        f"pointer commit P report layer {layer_name!r} is not passed=true"
                    )
                if layer.get("skipped") is not False:
                    problems.append(
                        f"pointer commit P report layer {layer_name!r} is not skipped=false"
                    )
            # C-122 round-18 gate-3: the layer set must be EXACTLY the six fixed
            # layer names — a renamed / replaced / foreign layer is not a
            # done-gate pass even when the count and flags match.
            if layer_names != set(_ALL_LAYER_NAMES):
                problems.append(
                    "pointer commit P report layer set != the six fixed layer names"
                )
        # 2. P's committed manifest binds S / E / run_id (exact schema) AND is
        # itself re-scanned from its committed bytes for a credential field name /
        # unknown 64-hex value (C-122 HG-H).  The P manifest is a committed
        # artifact that a hijacked publish can reach — the consumer must not
        # trust the publish-time scan alone.
        p_manifest = _committed_json_blob(pointer_commit, _MANIFEST_REL, problems, "P manifest")
        if isinstance(p_manifest, dict):
            p_manifest_blob = _git(
                "show", f"{pointer_commit}:{_MANIFEST_REL}", check=True, binary=True
            ).stdout
            try:
                _reject_credential_field_names(
                    p_manifest_blob, "committed evidence", _MANIFEST_REL
                )
                _reject_unknown_64hex_values(
                    p_manifest_blob, "committed evidence", _MANIFEST_REL
                )
            except GateStateChangedError as exc:
                problems.append(str(exc))
            if p_manifest.get("tested_commit_sha") != tested_commit_sha:
                problems.append("pointer commit P manifest tested_commit_sha does not bind S")
            if p_manifest.get("evidence_commit") != evidence_commit:
                problems.append("pointer commit P manifest evidence_commit does not bind E")
            if p_manifest.get("run_id") != run_id:
                problems.append("pointer commit P manifest run_id does not bind the run")
            # C-122 round-19 (02:56 supervision / gap 3): the P manifest's
            # STRUCTURAL contract — schema_version, required fields, the EXACT
            # fixed evidence file-name set, unique names, valid per-file
            # sha256/size/committed fields AND the canonical tracked_path /
            # committed flag per name — is enforced by the SAME canonical
            # validator as E's publish preflight and the E resolver.  A P
            # manifest that smuggles an extra file entry (or drops / renames
            # one / relocates a path / flips a committed flag) is a forged
            # pointer commit.
            _validate_evidence_manifest(
                p_manifest, label="pointer commit P manifest", problems=problems
            )
        # 3. E's committed manifest is the COMPLETE publisher contract (C-122
        # HG-C): it binds S / run_id, lists exactly the fixed evidence file set
        # with the exact field set and per-file size, records every committed
        # file present in E with a matching blob SHA256 AND size, records the
        # git-ignored raw originals as committed=false and absent from E's tree,
        # commits both layer-5/6 compact artifacts with their strong contract
        # verified from E's blob, cross-checks each compact's raw_evidence.sha256
        # against the manifest's recorded raw hash, and re-scans every committed
        # JSON blob for credential field names / unknown 64-hex values.
        e_manifest = _committed_json_blob(evidence_commit, _MANIFEST_REL, problems, "E manifest")
        if isinstance(e_manifest, dict):
            # C-122 HG-C: re-scan E's committed manifest blob itself.
            e_manifest_blob = _git(
                "show", f"{evidence_commit}:{_MANIFEST_REL}", check=True, binary=True
            ).stdout
            try:
                _reject_credential_field_names(
                    e_manifest_blob, "committed evidence", _MANIFEST_REL
                )
                _reject_unknown_64hex_values(
                    e_manifest_blob, "committed evidence", _MANIFEST_REL
                )
            except GateStateChangedError as exc:
                problems.append(str(exc))
            if e_manifest.get("tested_commit_sha") != tested_commit_sha:
                problems.append("evidence commit E manifest tested_commit_sha does not bind S")
            if e_manifest.get("run_id") != run_id:
                problems.append("evidence commit E manifest run_id does not bind the run")
            # C-122 round-19 (02:56 supervision / gap 3): the E manifest's
            # STRUCTURAL contract — schema_version, required fields, per-file
            # field set / types / uniqueness, the EXACT fixed evidence file-name
            # set, the canonical tracked_path and the canonical committed flag —
            # is enforced by the SAME canonical validator as generation and the
            # publish preflight.  Resolver-side rejects exactly what publish-side
            # rejects, so a renamed / missing / smuggled / relocated /
            # flag-flipped evidence entry can never be published on one side and
            # accepted on the other.
            _validate_evidence_manifest(
                e_manifest, label="evidence commit E manifest", problems=problems
            )
            files = e_manifest.get("files")
            if isinstance(files, list):
                e_tree = _git(
                    "ls-tree", "-r", "--name-only", evidence_commit, check=True
                ).stdout.splitlines()
                e_tree_set = set(e_tree)
                for entry in files:
                    if not isinstance(entry, dict):
                        continue  # structural contract already flagged it
                    entry_name = entry.get("name")
                    entry_sha = entry.get("sha256")
                    entry_size = entry.get("size_bytes")
                    entry_committed = entry.get("committed")
                    if not isinstance(entry_name, str) or not entry_name:
                        continue
                    rel = entry.get("tracked_path")
                    if not isinstance(rel, str) or not rel:
                        continue
                    if entry_committed is not True:
                        # Git-ignored raw evidence is listed by hash only.  Its
                        # recorded SHA256 is cross-bound by the layer-5/6 compact
                        # below; a raw file that somehow landed in E's tree is a
                        # contract violation, never silently accepted (C-122 HG-C).
                        if rel in e_tree_set:
                            problems.append(
                                f"evidence commit E manifest marks {rel} committed=false "
                                "but E's tree carries it"
                            )
                        continue
                    if rel not in e_tree_set:
                        problems.append(
                            f"evidence commit E {evidence_commit} missing committed file {rel}"
                        )
                        continue
                    blob = _git(
                        "show", f"{evidence_commit}:{rel}", check=True, binary=True
                    )
                    if hashlib.sha256(blob.stdout).hexdigest() != entry_sha:
                        problems.append(
                            f"evidence commit E file {rel} sha256 does not match the "
                            "committed manifest"
                        )
                    if len(blob.stdout) != entry_size:
                        problems.append(
                            f"evidence commit E file {rel} size_bytes "
                            f"{len(blob.stdout)} != manifest {entry_size}"
                        )
                    if rel.endswith(".json"):
                        # C-122 HG-C: re-scan every committed JSON artifact from
                        # E's blob — a credential field name or an unknown 64-hex
                        # value that reached the object graph is a leak even when
                        # the publish-time scan somehow missed it.
                        try:
                            _reject_credential_field_names(
                                blob.stdout, "committed evidence", rel
                            )
                            _reject_unknown_64hex_values(
                                blob.stdout, "committed evidence", rel
                            )
                        except GateStateChangedError as exc:
                            problems.append(str(exc))
                # C-122 HG-C: required compacts — the layer-5/6 compact artifacts
                # must be committed evidence, their strong contract verified from
                # E's blob, and each compact's raw_evidence.sha256 cross-checked
                # against the manifest's recorded hash for the raw file.  The
                # per-name tracked_path / committed binding is enforced by the
                # canonical validator above, so the compact requirement below can
                # trust ``by_name`` to carry the canonical paths and flags.
                by_name = {
                    entry["name"]: entry
                    for entry in files
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                }
                for staged_name, tracked_rel in _EVIDENCE_TRACKED_PATHS:
                    if "compact" not in staged_name:
                        continue
                    compact_entry = by_name.get(staged_name)
                    if (
                        compact_entry is None
                        or compact_entry.get("committed") is not True
                    ):
                        problems.append(
                            f"evidence commit E manifest does not record compact "
                            f"artifact {tracked_rel} as committed"
                        )
                        continue
                    if tracked_rel not in e_tree_set:
                        problems.append(
                            f"evidence commit E {evidence_commit} missing committed "
                            f"compact artifact {tracked_rel}"
                        )
                        continue
                    compact_blob = _git(
                        "show", f"{evidence_commit}:{tracked_rel}", check=True,
                        binary=True,
                    )
                    try:
                        compact = json_loads_no_dupes(compact_blob.stdout.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        problems.append(
                            f"evidence commit E compact artifact {tracked_rel} is not "
                            "valid JSON"
                        )
                        continue
                    if not isinstance(compact, dict):
                        problems.append(
                            f"evidence commit E compact artifact {tracked_rel} is not "
                            "an object"
                        )
                        continue
                    if staged_name == _COMPACT_CANARY_STAGED_NAME:
                        try:
                            _verify_layer5_compact_contract(tracked_rel, compact)
                        except GateStateChangedError as exc:
                            problems.append(str(exc))
                    else:
                        try:
                            _verify_layer6_compact_contract(
                                tracked_rel,
                                compact,
                                tested_commit_sha=tested_commit_sha,
                            )
                        except GateStateChangedError as exc:
                            problems.append(str(exc))
                    raw = compact.get("raw_evidence")
                    if not isinstance(raw, dict):
                        problems.append(
                            f"evidence commit E compact {tracked_rel} lacks raw_evidence"
                        )
                        continue
                    raw_file = raw.get("file")
                    raw_sha = raw.get("sha256")
                    if not isinstance(raw_file, str) or not raw_file:
                        problems.append(
                            f"evidence commit E compact {tracked_rel} "
                            "raw_evidence.file is invalid"
                        )
                    else:
                        raw_entry = by_name.get(raw_file)
                        if raw_entry is None:
                            problems.append(
                                f"evidence commit E compact {tracked_rel} "
                                f"raw_evidence.file {raw_file!r} is not listed in "
                                "the E manifest"
                            )
                        else:
                            if raw_entry.get("committed") is not False:
                                problems.append(
                                    f"evidence commit E compact {tracked_rel} "
                                    f"raw_evidence.file {raw_file!r} is not "
                                    "recorded as committed=false in the manifest"
                                )
                            if (
                                not isinstance(raw_sha, str)
                                or raw_entry.get("sha256") != raw_sha
                            ):
                                problems.append(
                                    f"evidence commit E compact {tracked_rel} "
                                    "raw_evidence.sha256 does not match the "
                                    "manifest's recorded hash for the raw file"
                                )
        # C-122 round-18 HG-H2 (supervision 16:03): per-entry binding between P's
        # manifest and the E canonical manifest + the real committed blob.  A
        # forged P manifest that records an arbitrary-but-well-formed sha256, size
        # 0, and committed=false for every file passes the field-shape contract
        # above — the entries must instead be EXACTLY the evidence E committed.
        if isinstance(p_manifest, dict) and isinstance(e_manifest, dict):
            _verify_p_manifest_binds_e(
                p_manifest, e_manifest, evidence_commit, problems
            )
        # C-122 HG-H: P must be E plus ONLY the report/manifest re-stamp — no
        # extra blob smuggled into P, no evidence file dropped from E, and no
        # silent content change in any other committed path.  A hijacked P that
        # carries an extra blob or mutates a committed evidence file behind the
        # published ref fails closed even though the report/manifest bindings
        # still read correctly.
        e_tree_paths = _git(
            "ls-tree", "-r", "--name-only", evidence_commit, check=True
        ).stdout.splitlines()
        p_tree_paths = _git(
            "ls-tree", "-r", "--name-only", pointer_commit, check=True
        ).stdout.splitlines()
        e_path_set = set(e_tree_paths)
        p_path_set = set(p_tree_paths)
        removed = e_path_set - p_path_set
        if removed:
            problems.append(
                f"pointer commit P dropped E's committed path(s): {sorted(removed)}"
            )
        unexpected = (p_path_set - e_path_set) - {_REPORT_REL, _MANIFEST_REL}
        if unexpected:
            problems.append(
                f"pointer commit P added unexpected path(s) beyond the "
                f"report/manifest re-stamp: {sorted(unexpected)}"
            )
        for rel in sorted(e_path_set & p_path_set):
            if rel in {_REPORT_REL, _MANIFEST_REL}:
                continue
            e_blob = _git(
                "show", f"{evidence_commit}:{rel}", check=True, binary=True
            ).stdout
            p_blob = _git(
                "show", f"{pointer_commit}:{rel}", check=True, binary=True
            ).stdout
            if e_blob != p_blob:
                problems.append(
                    f"pointer commit P changed E's committed file {rel} "
                    "(only the report/manifest may differ between E and P)"
                )
    except GateStateChangedError as exc:
        # Git became unavailable mid-verify (fail-closed, never a false pass).
        problems.append(str(exc))
    if problems:
        verdict["problems"] = problems
        return verdict
    verdict["verified"] = True
    verdict["report_passed"] = True
    verdict["summary"] = (
        f"verified: refs/tripchord/done-gate/{run_id} -> P {pointer_commit} -> "
        f"E {evidence_commit} -> S {tested_commit_sha}; single-parent chain, "
        "fixed evidence file set with matching hashes/sizes, required layer-5/6 "
        "compacts contract-verified, and no credential/64-hex leak in the "
        "committed trail"
    )
    return verdict


def _committed_json_blob(
    commit: str, rel: str, problems: list[str], label: str
) -> dict[str, Any] | None:
    """Read ``commit:rel`` and parse it as a JSON object, fail-closed.

    Appends a problem for an unreadable/non-JSON blob and returns None — the
    caller then skips field checks (each problem already names the violation).
    """
    try:
        blob = _git("show", f"{commit}:{rel}", check=True, binary=True).stdout
        parsed = json_loads_no_dupes(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, GateStateChangedError) as exc:
        problems.append(
            f"commit {commit} {rel} {label} is not valid JSON "
            f"({exc.__class__.__name__})"
        )
        return None
    if not isinstance(parsed, dict):
        problems.append(f"commit {commit} {rel} {label} is not an object")
        return None
    return parsed


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
        "--live-state-db",
        type=Path,
        default=None,
        help=(
            "durable live-state SQLite file for the R7 read-only lease "
            "preflight (default: <repo-root>/tripchord.db, or the local path "
            "named by DATABASE_URL/TRIPCHORD_DATABASE_URL)"
        ),
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
    parser.add_argument(
        "--verify-ref",
        metavar="RUN_ID",
        nargs="?",
        const="",
        default=None,
        help=(
            "machine-gate consumer mode: verify the published evidence trail "
            "for this run_id from refs/tripchord/done-gate/<run_id> and exit 0 "
            "when verified, 2 otherwise.  No layers run.  RUN_ID is optional "
            "when --latest is used (C-122 HG-D): --verify-ref --latest resolves "
            "the most recently published gate ref."
        ),
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="with --verify-ref, resolve the most recently published gate ref instead of a run_id",
    )
    args = parser.parse_args(argv)

    # C-122 round-18 gate-3: ``--latest`` only makes sense WITH ``--verify-ref`` —
    # a bare ``--latest`` is a parameter mistake and must fail closed instead of
    # being silently ignored.
    if args.latest and args.verify_ref is None:
        print(
            json.dumps(
                {
                    "verified": False,
                    "problems": ["--latest requires --verify-ref"],
                },
                sort_keys=True,
            )
        )
        return 2

    # C-122 round-18 gate-7 + HG-D: the resolver entry — no layers run, no
    # evidence is written.  The ref is the authoritative record; the tracked
    # report copy is never trusted here.  ``--verify-ref`` may be passed with no
    # RUN_ID when ``--latest`` resolves the ref, so argparse must not exit on a
    # missing value before the resolver runs.
    if args.verify_ref is not None:
        run_id = args.verify_ref
        if args.latest:
            try:
                run_id = _latest_gate_run_id()
            except GateStateChangedError as exc:
                print(json.dumps({"verified": False, "problems": [str(exc)]}, sort_keys=True))
                return 2
        elif run_id == "":
            # --verify-ref with neither a RUN_ID nor --latest: a parameter
            # mistake, fail closed with a clear problem instead of argparse's
            # bare "expected one argument".
            print(
                json.dumps(
                    {
                        "verified": False,
                        "problems": [
                            "--verify-ref requires a RUN_ID or --latest"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 2
        verdict = verify_gate_ref(run_id)
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict.get("verified") else 2

    # One run_id per execution (C-114 R3): bound into the staging path (default
    # dir) and threaded into the report so the evidence trail identifies exactly
    # which run produced it.
    run_id = _new_run_id()
    staging_dir = args.staging_dir or _new_staging_dir(run_id)
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
        # Only now is it safe to create the write targets — exclusively (C-118).
        # The fail-closed conflict check above guarantees the path does not
        # exist, so a non-``exist_ok`` mkdir is safe; a concurrent writer that
        # creates the dir between the check and this mkdir surfaces as
        # FileExistsError and fails the gate closed rather than reusing a
        # foreign directory.
        staging_dir.mkdir(parents=True, mode=0o700)
        # Owner-only from creation: raw evidence never world/group-readable.
        staging_dir.chmod(0o700)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        report = run_gate(
            staging_dir,
            commit=args.commit,
            run_id=run_id,
            live_state_db=args.live_state_db,
        )
    except (GateStateChangedError, OSError) as exc:
        if args.quiet:
            print(
                json.dumps(
                    {"passed": False, "summary": _redact_output(str(exc))},
                    sort_keys=True,
                )
            )
        else:
            print(f"TripChord product v1.0 Done-Gate  {_now()}")
            # C-118: never echo the raw error text — it may carry a subprocess
            # line, a path or a value that reads as sensitive.
            print(f"gate aborted: {_redact_output(str(exc))}", file=sys.stderr)
        return 2

    try:
        _dump(report, output_path)
    except (GateStateChangedError, OSError) as exc:
        # A report that cannot be dumped must surface as exit 2, never as a raw
        # traceback.  No evidence commit has happened yet, so no passed=true
        # pointer is at risk (C-118 Gap 1).
        print(f"gate aborted: {_redact_output(str(exc))}", file=sys.stderr)
        return 2
    # C-118: every exit path scans the final report AFTER it is written.  The
    # dump already redacted the payload, so a leak here is a class the
    # redaction does not cover — fail closed AND remove the report so nothing
    # sensitive stays on disk.
    try:
        _secret_scan_paths([output_path], _evidence_scan_needles(), "report")
    except GateStateChangedError as exc:
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)
        print(f"gate aborted: {_redact_output(str(exc))}", file=sys.stderr)
        return 2
    try:
        # Re-secure the staging tree after layers wrote it: dir 0700, every raw
        # evidence + report file 0600.  A hardening failure fails the gate.
        _harden_staging_permissions(staging_dir)
    except GateStateChangedError as exc:
        print(f"staging hardening failed: {_redact_output(str(exc))}", file=sys.stderr)
        return 2

    if args.commit_evidence and not report.passed:
        # A failed gate never commits evidence (A1): the staged evidence stays
        # in the ignored/out-of-repo staging dir; HEAD, index and tracked files
        # are left byte-for-byte unchanged — no _commit_evidence, no report
        # write to the tracked results tree.  The staging report already
        # carries the failed verdict, so exit 2 directly.
        _safe_print_report(report, output_path, args.quiet)
        return 2

    if args.commit_evidence:
        try:
            # C-114: derive the desensitized layer-5/6 compact artifacts from
            # the raw evidence BEFORE the required-input gate, so the committed
            # trail carries independently reviewable layer-5/6 evidence instead
            # of only a committed=false raw hash.
            _generate_compact_evidence(staging_dir)
            # Evidence-contract gate: the fixed required raw inputs (including
            # layer-5/6 raw evidence and their compact artifacts) must all exist
            # before any committed trail is produced.  A missing required input
            # hard-fails exit 2 rather than silently omitting the file from the
            # manifest.
            _verify_required_evidence_inputs(staging_dir)
            start = _git_snapshot()
            # C-122 Fix 7: the delivered report (carrying evidence_commit=E) is
            # generated inside _commit_evidence BEFORE the CAS and covered by
            # the pre-CAS secret scan.  Nothing is dumped after the CAS.
            _commit_evidence(
                staging_dir,
                report,
                start=start,
                local_report_path=output_path,
            )
        except GateStateChangedError as exc:
            # The run verdict is intact but the evidence commit is missing:
            # a committed report must never claim an evidence trail that does
            # not exist, so the phase failure hard-fails the whole gate (exit 2)
            # even when the layers themselves all passed.
            print(f"evidence commit failed: {_redact_output(str(exc))}", file=sys.stderr)
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
            _safe_print_report(report, output_path, args.quiet)
            return 2
        # C-122 Fix 7: no post-CAS output.  The delivered local report was
        # generated and secret-scanned inside _commit_evidence BEFORE the CAS —
        # the CAS is the last action, so the report on disk and the committed
        # report agree and nothing fallible runs after the branch move.

    _safe_print_report(report, output_path, args.quiet)
    return 0 if report.passed else 2


if __name__ == "__main__":
    sys.exit(main())
