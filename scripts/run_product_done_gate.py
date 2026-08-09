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
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "benchmarks" / "results"
OUTPUT_PATH = RESULTS_DIR / "product-v1-done-gate.json"
EVIDENCE_SCHEMA = "tripchord-product-v1-done-gate"

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
    commit_sha: str | None
    worktree_dirty: bool
    layers: list[LayerResult]
    passed: bool
    summary: str
    boundary: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _worktree_dirty() -> bool:
    """Whether the repo has uncommitted changes.

    Done-Gate evidence must map 1:1 to a committed revision.  A dirty worktree
    means the code that actually ran differs from ``HEAD``, so a ``commit_sha``
    recorded via ``git rev-parse HEAD`` alone (the stale-HEAD bug this replaces)
    would silently point at code that was never exercised.  When the tree is
    dirty the gate forces ``passed=false`` and records ``worktree_dirty=true``
    instead of claiming a pass against an unverifiable revision.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        # Git unavailable means the tree's cleanliness cannot be proven; treat
        # the revision as unverifiable rather than guess at a pass.
        return True


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


def layer2_replay() -> LayerResult:
    """Replay-mode core benchmarks (no real OTA access)."""
    checks: list[dict[str, Any]] = []
    commands = (
        ("benchmarks.evaluate", "benchmark_verifier"),
        ("benchmarks.evaluate_planning", "benchmark_planning"),
        ("benchmarks.evaluate_repair", "benchmark_repair"),
        ("benchmarks.evaluate_events", "benchmark_events"),
        ("benchmarks.evaluate_acceptance", "acceptance_surfaces"),
    )
    for module, label in commands:
        code, out = _run(["uv", "run", "python", "-m", module], timeout=600)
        passed = code == 0
        checks.append({"name": label, "passed": passed, "detail": out[-300:] if not passed else ""})
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="2_replay",
        passed=passed,
        detail="verifier/planning/repair/events benchmarks",
        sub_checks=checks,
    )


def layer3_clean_chrome_fixtures() -> LayerResult:
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
    code4, out4 = _run(
        ["uv", "run", "python", "scripts/browser_e2e.py"],
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


def layer5_real_canary() -> LayerResult:
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

    evidence_path = ROOT / "benchmarks" / "results" / "live-canary-certified.json"
    code, out = _run(
        [
            "uv",
            "run",
            "python",
            "benchmarks/live_canary_certified.py",
            "--bridge-token",
            bridge_token,
        ],
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
                "benchmarks/results/live-canary-certified.json"
            )
        ),
        sub_checks=sub_checks,
    )


def layer6_full_e2e() -> LayerResult:
    """Full-platform real E2E only when every external condition is met.

    Runs ``benchmarks/run_live_done_gate_v4.py`` as the real executor: live job
    submit / wait / cancel, event replan, and the strict live-v4 gate
    evaluation.  The executor incurs live model cost (user-authorized via
    ``TRIPCHORD_ACK_MODEL_COST=1``) and requires the paired Companion; until
    those gates are met this layer honestly fails as pending user
    authorization — never forged.
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
    output_path = ROOT / "benchmarks" / "results" / "live-done-gate-v4.json"
    code, out = _run(
        [
            "uv",
            "run",
            "python",
            "benchmarks/run_live_done_gate_v4.py",
            "--api-base",
            "http://127.0.0.1:8000",
            "--bridge-token",
            bridge_token,
            "--require-model-enhancement",
            "--output",
            str(output_path),
        ],
        timeout=4500,
    )
    passed = code == 0
    return LayerResult(
        name="6_full_e2e",
        passed=passed,
        detail=(
            f"full-platform real E2E passed; evidence {output_path}"
            if passed
            else (
                "pending user authorization or executor failure: "
                f"run_live_done_gate_v4.py exited {code}; "
                f"{out[-300:]}"
            )
        ),
    )


def _applicable(layers: list[LayerResult]) -> list[LayerResult]:
    return [layer for layer in layers if not layer.skipped]


def run_gate(*, commit: str | None) -> GateReport:
    # Snapshot the tree state BEFORE any layer runs: layers legitimately rewrite
    # tracked evidence bundles (e.g. layer 5's live-canary-certified.json), which
    # would otherwise look like uncommitted changes.  The invariant that matters
    # is that the code actually exercised was the committed HEAD when the gate
    # began — evidence files written during the run are outputs, not running code.
    worktree_dirty = _worktree_dirty()
    layers = [
        layer1_reproducibility(),
        layer2_replay(),
        layer3_clean_chrome_fixtures(),
        layer4_model_smoke(),
        layer5_real_canary(),
        layer6_full_e2e(),
    ]
    applicable = _applicable(layers)
    passed = (
        not worktree_dirty
        and bool(applicable)
        and all(layer.passed for layer in applicable)
    )
    if worktree_dirty:
        summary = (
            "worktree has uncommitted changes; running code differs from HEAD, "
            "so commit_sha cannot name the code that was exercised — commit the "
            "tree and re-run before this evidence can be accepted"
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
    )
    return GateReport(
        schema_version=EVIDENCE_SCHEMA,
        generated_at=_now(),
        commit_sha=commit,
        worktree_dirty=worktree_dirty,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print the verdict")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="atomic output JSON path",
    )
    args = parser.parse_args()

    report = run_gate(commit=_commit_sha())
    output_path = _dump(report, args.output)

    if args.quiet:
        print(
            json.dumps(
                {"passed": report.passed, "summary": report.summary},
                sort_keys=True,
            )
        )
        return 0 if report.passed else 2

    print(f"TripChord product v1.0 Done-Gate  {report.generated_at}")
    print(f"commit: {report.commit_sha or 'unknown'}")
    print(f"worktree_dirty: {report.worktree_dirty}")
    for layer in report.layers:
        marker = "PASS" if layer.passed else ("SKIP" if layer.skipped else "FAIL")
        print(f"  [{marker}] {layer.name}  {layer.detail}")
    print(f"\nverdict: {report.summary}")
    print(f"boundary: {report.boundary}")
    print(f"evidence: {output_path}")
    return 0 if report.passed else 2


if __name__ == "__main__":
    sys.exit(main())
